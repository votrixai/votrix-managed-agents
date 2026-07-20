# Google Cloud Run deployment

This is the canonical deployment path for Votrix Managed Agents. It mirrors the
`votrix-backend` production/staging workflow while using a dedicated VMA runtime
identity and a migration gate before every API-and-worker rollout.

## Fixed layout

| Setting | Value |
|---|---|
| Project | `votrixai-480422` |
| Region | `us-central1` |
| Artifact Registry | `votrix` |
| Production API service | `votrix-managed-agents` |
| Production worker service | `votrix-managed-agents-worker` |
| Staging API service | `votrix-managed-agents-staging` |
| Staging worker service | `votrix-managed-agents-staging-worker` |
| Runtime service account | `vma-runtime@votrixai-480422.iam.gserviceaccount.com` |
| Production Cloud Tasks queue | `us-central1/vma-turns` |
| Staging Cloud Tasks queue | `us-central1/vma-turns-staging` |

Each environment is split into an API service and a worker service. API
instances accept HTTP/SSE traffic but never execute queued Agent turns. Worker
instances expose a private OIDC turn endpoint plus health endpoints. Each worker
admits at most five in-flight turn requests and keeps one slow PostgreSQL
reconciler for expired leases and failed task creation. Both roles keep CPU
allocated, use one web process, and run the
same startup and database liveness probes. Each instance is pinned to one vCPU
and 4 GiB memory; API instances accept 40 concurrent HTTP requests while worker
instances use `containerConcurrency=5`, equal to the process turn limiter.

Production keeps one to three API instances and one to eight worker instances.
Staging keeps one to two API instances and one to two worker instances. Only the
API services disable the Cloud Run Invoker IAM check; the worker services stay
private. Cloud Tasks push requests drive worker autoscaling, while PostgreSQL
remains the source of truth and the reconciler preserves progress when dispatch
is unavailable. API request autoscaling and Agent-turn capacity are independent.

## Prerequisites

- Install and authenticate the Google Cloud CLI.
- Ensure your account can enable APIs, edit IAM, submit builds, and manage Cloud
  Run, Cloud Tasks, Artifact Registry, Secret Manager, and Cloud Build triggers.
- Use managed PostgreSQL. SQLite is not durable or multi-instance safe on Cloud
  Run.
- Keep development, staging, and production in three separate Supabase projects;
  these environments must never share a database. Runtime SQLAlchemy and
  LangGraph checkpoint traffic use the Supavisor transaction pooler on port
  `6543`. The preview listener and janitor advisory lock use a separate
  session-mode URL on port `5432`, while the migration Job receives its own
  session/direct secret. A transaction-mode pooler cannot hold a lifetime
  `LISTEN` connection or a session-scoped advisory lock. The local `.env` uses
  the development project; the two Secret Manager files below use staging and
  production.
- Build the operator-owned `vma-hardened` template in the E2B account before
  creating an E2B-backed session.

## One-time setup

Run the scripts in order:

```bash
./scripts/gcloud/0-setup-registry.sh
```

This enables the required APIs, creates the Artifact Registry and dedicated
runtime service account, and grants Cloud Build permission to deploy as that
identity. Runtime access to secrets is granted per secret by the next step.

Create the Cloud Tasks queues and grant the runtime identity permission to
enqueue OIDC tasks:

```bash
./scripts/gcloud/8-setup-cloud-tasks.sh all
```

On the first run, the worker services do not exist yet, so the command configures
the queues, Enqueuer role, the Cloud Tasks primary service-agent role, and both
OIDC `iam.serviceAccounts.actAs` bindings, then reports that worker Invoker
bindings are pending. This is expected. The deploy
scripts create a missing worker once in `poll` mode, query its real Cloud Run
URL, grant the runtime identity `roles/run.invoker` on that private service, and
only then render the final `hybrid` worker and API revisions with that URL.
Rerunning the setup command remains an idempotent IAM/queue repair path. No
guessed URL or Secret Manager placeholder is used.

Create the two untracked Secret Manager input files from the checked-in
templates, then replace every placeholder:

```bash
cp .env.production.example .env.production
cp .env.staging.example .env.staging
```

Each unquoted `KEY=value` file contains exactly these required values:

```env
DATABASE_URL=
VMA_LISTEN_DATABASE_URL=
DATABASE_URL_DIRECT=
VMA_SUPABASE_URL=
VMA_SUPABASE_PUBLISHABLE_KEY=
VMA_RESEND_API_KEY=
VMA_ENCRYPTION_KEY=
E2B_API_KEY=
S3_ENDPOINT_URL=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_BUCKET_NAME=
```

Import them into Secret Manager:

```bash
./scripts/gcloud/1-create-secrets.sh .env.production
./scripts/gcloud/1-create-secrets.sh .env.staging --suffix staging
```

The importer has an explicit allowlist. It ignores unrelated values and creates
only these names:

| Environment variable | Production secret | Staging secret |
|---|---|---|
| `DATABASE_URL` | `vma-database-url` | `vma-database-url-staging` |
| `VMA_LISTEN_DATABASE_URL` | `vma-listen-database-url` | `vma-listen-database-url-staging` |
| `DATABASE_URL_DIRECT` | `vma-database-url-direct` | `vma-database-url-direct-staging` |
| `VMA_SUPABASE_URL` | `vma-supabase-url` | `vma-supabase-url-staging` |
| `VMA_SUPABASE_PUBLISHABLE_KEY` | `vma-supabase-publishable-key` | `vma-supabase-publishable-key-staging` |
| `VMA_RESEND_API_KEY` | `vma-resend-api-key` | `vma-resend-api-key-staging` |
| `VMA_ENCRYPTION_KEY` | `vma-encryption-key` | `vma-encryption-key-staging` |
| `E2B_API_KEY` | `vma-e2b-api-key` | `vma-e2b-api-key-staging` |
| `S3_ENDPOINT_URL` | `vma-s3-endpoint-url` | `vma-s3-endpoint-url-staging` |
| `S3_ACCESS_KEY_ID` | `vma-s3-access-key-id` | `vma-s3-access-key-id-staging` |
| `S3_SECRET_ACCESS_KEY` | `vma-s3-secret-access-key` | `vma-s3-secret-access-key-staging` |
| `S3_BUCKET_NAME` | `vma-s3-bucket-name` | `vma-s3-bucket-name-staging` |

The API and worker manifests set `VMA_CHECKPOINT_DATABASE_URL` explicitly from
the same transaction-pooler secret as `DATABASE_URL`. They set
`VMA_LISTEN_DATABASE_URL` from the session-mode secret. The migration Job alone
maps `vma-database-url-direct[-staging]` to its `DATABASE_URL`.

The Supabase URL and publishable key enable hosted owner and superadmin JWT
authentication. They must match the Votrix web application in each environment;
never substitute the Supabase service-role key.

`VMA_RESEND_API_KEY` sends Organization owner invitations. It is mounted only
into the API service; workers do not send invitation email. The API manifests
pin the matching Developer Console URL, the verified
`Votrix <no-reply@mail.votrixai.com>` sender, and a 14-day invitation lifetime.

Do not quote values in these files, and do not commit them.

The object-storage bucket must remain private. VMA uses its scoped S3
credentials for server-side reads and writes, issues short-lived presigned PUTs
for direct browser uploads, and serves downloads through the authenticated
`/v1/files/{file_id}/content` endpoint. For Cloudflare R2, leave both the
`r2.dev` development URL and public custom domains disabled. Browser uploads
using presigned URLs still require a bucket CORS policy for the application
origins; CORS does not make the bucket public.

The platform-level default route keeps the static latency-first
Fireworks/Together OpenRouter policy, but it does not include a shared model
key. Anthropic, OpenAI, DeepSeek, OpenRouter, and operator-registered providers
resolve every key from a Session-mounted Organization Vault model Credential.
Model API keys do not belong in Cloud Run environment variables, Secret
Manager deployment inputs, or `VMA_MODEL_PROVIDERS`.

The Cloud Run manifests pin these non-secret runtime settings rather than
loading them from Secret Manager. Both roles share the governance, Session,
model, E2B, and storage settings. API-specific settings are:

```env
VMA_SERVICE_ROLE=api
VMA_EMBEDDED_WORKER_ENABLED=false
VMA_DB_POOL_SIZE=4
VMA_DB_MAX_OVERFLOW=2
VMA_CONSOLE_BASE_URL=https://vma.votrixai.com
VMA_EMAIL_FROM=Votrix <no-reply@mail.votrixai.com>
VMA_ORGANIZATION_INVITE_TTL_DAYS=14
```

Worker-specific settings are:

```env
VMA_SERVICE_ROLE=worker
VMA_EMBEDDED_WORKER_ENABLED=true
VMA_WORKER_TURN_LIMIT=5
VMA_WORKER_CONCURRENCY=1
VMA_WORKER_POLL_INTERVAL_SECONDS=20
VMA_WORKER_LEASE_SECONDS=120
VMA_WORK_MAX_ATTEMPTS=3
VMA_CHECKPOINT_POOL_MAX_SIZE=3
VMA_DB_POOL_SIZE=4
VMA_DB_MAX_OVERFLOW=1
```

Shared hosted settings include:

```env
VMA_EVENT_POLL_INTERVAL_SECONDS=1.0
VMA_PREVIEW_BROKER=pg_notify
VMA_WORK_DISPATCH_MODE=hybrid
VMA_TASKS_QUEUE=vma-turns[-staging]
VMA_TASKS_LOCATION=us-central1
VMA_TASKS_SERVICE_ACCOUNT=vma-runtime@votrixai-480422.iam.gserviceaccount.com
VMA_WORKER_URL=<discovered private Cloud Run worker URL>
VMA_MAX_SESSION_INPUT_BYTES=67108864
VMA_DB_POOL_TIMEOUT_SECONDS=10
VMA_DB_POOL_RECYCLE_SECONDS=300
VMA_REQUESTS_PER_MINUTE=600
VMA_MAX_ACTIVE_WORK=20
VMA_ORGANIZATION_STORAGE_BYTES=5368709120
VMA_PUBLIC_GA_ONLY=true
VMA_CORS_ORIGINS=https://<matching-vma-developer-app>,https://docs.vma.votrixai.com
```

The 64 MiB aggregate Session-input cap bounds create-time materialization and
one-time E2B injection. E2B turns resume from the sealed filesystem and do not
rehydrate all inputs from R2. Runtime and checkpoint pools use transaction mode,
so their client connections do not each pin a scarce Postgres backend. Every API
process keeps one session-mode `LISTEN` connection, and a janitor leader holds a
transient session-mode advisory-lock connection. Worker publishers use their
existing SQLAlchemy pool and do not add a dedicated listener connection.
PostgreSQL preview delivery is best-effort; SSE
clients reconcile dropped or missed frames against durable Session events. The
hosted Organization defaults admit bursts of up to 20 queued/running turns.
Each worker admits five turns. Production starts with five warm execution slots
and may scale to forty only after the production Supabase connection budget is
measured and the release gate in the scaling runbook is satisfied.

## Manual deploys

Run the read-only readiness check first. It verifies the enabled APIs,
Artifact Registry, Cloud Build IAM, runtime identity, enabled secret versions,
per-secret access, the target manifest, git state, and release branches without
reading any secret value:

```bash
./scripts/gcloud/preflight.sh all
```

For an intentional dirty staging experiment, use
`./scripts/gcloud/preflight.sh staging --allow-dirty`; production preflight and
deployment never accept dirty source. E2B template existence and the actual
Postgres/R2/E2B credentials remain runtime checks, so preflight does not replace
the migration Job or staging acceptance smoke.

```bash
./scripts/gcloud/2-deploy-production.sh
./scripts/gcloud/3-deploy-staging.sh
```

Both commands require a clean git worktree, including no staged or untracked
files. Production never permits an override, because a commit-looking image tag
must identify exactly the committed source that was built. For an intentional
staging-only experiment, use:

```bash
./scripts/gcloud/3-deploy-staging.sh --allow-dirty
```

That opt-in image receives a unique
`<commit>-dirty-<UTC timestamp>-<process>` tag; it cannot be mistaken for the
clean commit image. Either script also accepts a legacy positional region or
`--region=REGION`.

Both scripts enforce the same sequence:

1. Build and push a commit-tagged image.
2. Deploy or update `<service>-migrate` with that exact image.
3. execute `sh scripts/migrate.sh` as a Cloud Run Job and wait for success.
4. If the worker does not exist, create it in `poll` mode and query the URL that
   Cloud Run actually assigned.
5. Grant the runtime OIDC identity `roles/run.invoker` on the private worker.
6. Render and replace the private worker in `hybrid` mode with that URL.
7. Render and replace the API service in `hybrid` mode only after the worker is
   ready.

After the first deployment of an environment, verify the final state:

```bash
./scripts/gcloud/preflight.sh staging
```

Use `production` for the production environment after its connection and load
gates are cleared. Subsequent deploys discover the existing worker URL and need
no bootstrap revision.

The web entrypoint has no migration branch, so restarts never race to run
Alembic themselves.

## Scaling Agent-turn capacity

The full capacity, connection-budget, rollout, rollback, and incident procedure
is in the private [scaling runbook](../../private-docs/scaling-runbook.md). The
summary below is sufficient only for routine worker-count adjustments.

API capacity and Agent-turn capacity scale independently. Cloud Run scales the
API service from HTTP/SSE request load. Named Cloud Tasks send one authenticated
request per durable work item, so in-flight turn requests drive worker scale-out.
The process limiter and Cloud Run concurrency use the same bound:

```text
turn execution capacity = worker instances × VMA_WORKER_TURN_LIMIT
```

The checked-in manifests use `VMA_WORKER_TURN_LIMIT=5`, `containerConcurrency=5`,
and one slow reconciler coroutine per instance. Production keeps one warm worker
and permits at most eight instances, for 5–40 turns. Staging permits one or two,
for 5–10 turns. The production maximum is not permission to deploy blindly:
the first production release remains blocked until the Supabase compute tier,
pooler client ceiling, transaction-pool backend budget, and operational
headroom are recorded in the scaling runbook.

Cloud Tasks is never the work ledger. Task creation failure is logged and the
20-second PostgreSQL reconciler eventually claims the queued item. Pausing or
deleting a queue therefore slows dispatch but does not lose work. Queue retry
policy is `maxAttempts=8`, 5–300 second backoff, and at most 25 concurrent
dispatches. Each task sets its own 1,800-second dispatch deadline in application
code; there is deliberately no queue-level deadline setting.

Before raising `maxScale` or the per-instance turn limit, recalculate PostgreSQL,
model-provider, E2B, CPU, memory, and spend budgets. A manual Cloud Run scaling
change is temporary because the next manifest replacement restores the
checked-in bounds. Validate changes with `scripts/performance_smoke.py` in
staging and update the manifest, static tests, and runbook together.

## Staging acceptance gates

After staging deploys, run the controlled one-Session acceptance first:

```bash
VMA_SMOKE_BASE_URL=https://YOUR-STAGING-CLOUD-RUN-URL \
VMA_SMOKE_API_KEY=... \
VMA_SMOKE_VAULT_IDS=vault_... \
uv run --extra sandbox-e2b python scripts/pilot_acceptance.py
```

`VMA_SMOKE_VAULT_IDS` must reference an existing staging Vault containing the
model Credential selected by the smoke Agent. The hosted service intentionally
has no platform model API key, so the acceptance would otherwise fail closed
with `model_credential_required` before exercising E2B or R2.

Then use an existing staging Vault containing the selected model Credential to
run the ten-Session burst:

```bash
VMA_PERF_BASE_URL=https://YOUR-STAGING-CLOUD-RUN-URL \
VMA_PERF_API_KEY=... \
VMA_PERF_VAULT_IDS=vault_... \
uv run python scripts/performance_smoke.py
```

The performance smoke creates disposable Sessions and, unless existing IDs are
provided, a disposable Agent and Environment. It never modifies a supplied
Vault, Agent, or Environment; cleanup deletes Sessions and the Environment and
archives the Agent. It reports provision, trigger, queue, first-event, and
total latency with p50/p95/max summaries. The run makes real model and E2B calls
and therefore consumes the corresponding provider quotas.

For the first controlled rollout, the GCP wrapper can migrate an existing
operator-owned model-key Secret Manager value into an encrypted VMA Vault and
run the one-Session smoke without exposing either credential:

```bash
VMA_SMOKE_MODEL_API_KEY_SECRET=YOUR_OPERATOR_MODEL_KEY_SECRET \
  ./scripts/gcloud/7-run-acceptance.sh staging
```

The named source is never mounted into Cloud Run and is used only by the
trusted operator command. After the Vault-backed smoke passes, retire any
transitional model-key secret; the active encrypted Credential belongs to the
staging Organization Vault.

## Bootstrap the first tenant API key

Authentication is database-backed in every environment. After the first
successful migration, the recommended GCP path pre-provisions an operator key
in Secret Manager and idempotently stores only its digest in VMA's database:

```bash
./scripts/gcloud/6-bootstrap-operator.sh staging
./scripts/gcloud/6-bootstrap-operator.sh production
```

These create `vma-operator-api-key-staging` and `vma-operator-api-key`. Neither
secret is mounted into Cloud Run; they are operator/client credentials, not a
shared runtime authentication bypass. The script never prints the plaintext
key and can be retried safely with the same pre-provisioned value.

For a non-GCP secret sink, run the lower-level bootstrap CLI once from a trusted
operator machine using the matching untracked environment file:

```bash
set -a
. ./.env.production
set +a
uv run python -m scripts.bootstrap_api_key \
  --organization-id org_votrix \
  --organization-slug votrix \
  --organization-name "Votrix"
unset DATABASE_URL
```

Use `.env.staging` and a distinct Organization ID for staging. The command prints
the plaintext key exactly once; place it directly in the intended password
manager or client secret store. VMA persists only its digest. Do not redirect
the output into the repository or Cloud Run logs. Future key creation and
rotation use the authenticated `/v1/api_keys` API instead of this bootstrap
path.

## Automatic deploys

The checked-in setup uses the regional 2nd-gen Cloud Build repository
`us-central1/votrix-github/votrix-managed-agents`. The GitHub App installation
must grant access to `votrixai/votrix-managed-agents`. Once that one-time host
connection and repository link are complete, run:

```bash
./scripts/gcloud/4-setup-triggers.sh <github-owner> <repo-name>
```

This creates:

- `vma-deploy-production`: `main` → production, deployed automatically
- `vma-deploy-staging`: `staging` → staging, deployed automatically

The setup command is idempotent: it imports the complete desired trigger state,
patching either named trigger when it already exists and creating it otherwise.
This avoids the provider-specific `update github` path, which cannot reconcile a
2nd-gen `repositoryEventConfig`. The script also fails closed when the connection
is not `COMPLETE` or the linked repository points at a different GitHub remote.
Override the defaults only when intentionally migrating the Cloud Build source:

```bash
VMA_TRIGGER_REGION=us-central1 \
VMA_CLOUD_BUILD_CONNECTION=votrix-github \
VMA_CLOUD_BUILD_REPOSITORY=votrix-managed-agents \
  ./scripts/gcloud/4-setup-triggers.sh votrixai votrix-managed-agents
```

Regional triggers explicitly use the project's Compute Engine default service
account, matching the existing backend triggers and the IAM grants established
by `0-setup-registry.sh`. Set `VMA_CLOUD_BUILD_SERVICE_ACCOUNT` to another
service-account email only after granting the equivalent Artifact Registry,
Cloud Run, build, logging, and runtime-identity permissions.

If production should intentionally require a human approval gate, enable it
explicitly:

```bash
VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL=true \
  ./scripts/gcloud/4-setup-triggers.sh <github-owner> <repo-name>
```

The default is `false`. Both triggers ignore changes limited to `docs/**`,
`website/**`, `sdks/**`, `infra/cloudflare/**`, `README.md`, and
`CHANGELOG.md`, so documentation, SDK, and Cloudflare-router-only commits do
not rebuild the API image or run migrations. A commit that also changes any
non-ignored path still triggers the normal deployment.

Cloud Build uses the same manifests and the same blocking migration-job sequence
as manual deployment.

## Public API access

After both API services exist:

```bash
./scripts/gcloud/5-allow-public.sh
```

The API manifests already disable the Cloud Run Invoker IAM check. This command
is an idempotent repair/verification step using the same recommended Cloud Run
setting; it does not create an `allUsers` IAM binding. The helper rejects any
service name containing `worker`, and worker manifests do not disable the IAM
check. VMA still requires a database-backed Organization API key, so public API
ingress does not add an anonymous application path.

## Status

```bash
./scripts/gcloud/status.sh
```

The command prints each API and worker service URL, ready revision, immutable
image tag, migration-job image, and each Cloud Tasks queue's state, retry cap,
and concurrent-dispatch bound.

## Files

| File | Purpose |
|---|---|
| `cloudbuild.yaml` | Build, push, migrate, then deploy API and worker |
| `service.production.yaml` | Production API Cloud Run service |
| `service.worker.production.yaml` | Production worker Cloud Run service |
| `service.staging.yaml` | Staging API Cloud Run service |
| `service.worker.staging.yaml` | Staging worker Cloud Run service |
| `scripts/gcloud/config.sh` | Shared project and service names |
| `scripts/gcloud/0-setup-registry.sh` | APIs, registry, runtime identity, IAM |
| `scripts/gcloud/1-create-secrets.sh` | Allowlisted Secret Manager import |
| `scripts/gcloud/2-deploy-production.sh` | Manual production rollout |
| `scripts/gcloud/3-deploy-staging.sh` | Manual staging rollout |
| `scripts/gcloud/4-setup-triggers.sh` | GitHub Cloud Build triggers |
| `scripts/gcloud/5-allow-public.sh` | Repair the public Invoker IAM-check setting |
| `scripts/gcloud/6-bootstrap-operator.sh` | Securely bootstrap an operator API key to Secret Manager and Postgres |
| `scripts/gcloud/7-run-acceptance.sh` | Provision the BYOK smoke Vault and run real R2/E2B/model acceptance |
| `scripts/gcloud/8-setup-cloud-tasks.sh` | Idempotently configure queues and OIDC dispatch IAM |
| `scripts/gcloud/preflight.sh` | Read-only GCP, IAM, secret metadata, manifest, and git readiness |
| `scripts/gcloud/status.sh` | Deployed service and job status |
