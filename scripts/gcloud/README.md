# Google Cloud Run deployment

This is the canonical deployment path for Votrix Managed Agents. It mirrors the
`votrix-backend` production/staging workflow while using a dedicated VMA runtime
identity and a migration gate before every API-and-worker rollout.

## Fixed layout

| Setting | Value |
|---|---|
| Project | `votrixai-480422` |
| Production runtime region | `us-east4` (Northern Virginia, near Supabase AWS `us-east-1`) |
| Staging runtime region | `us-west2` (Los Angeles, near Supabase AWS `us-west-1`) |
| Cloud Build source region | `us-central1` |
| Artifact Registry | `us-east4/votrix` production; `us-west2/votrix` staging |
| Production API service | `votrix-managed-agents` |
| Production worker service | `votrix-managed-agents-worker` |
| Staging API service | `votrix-managed-agents-staging` |
| Staging worker service | `votrix-managed-agents-staging-worker` |
| Runtime service account | `vma-runtime@votrixai-480422.iam.gserviceaccount.com` |
| Production Cloud Tasks queue | `us-east4/vma-turns` |
| Staging Cloud Tasks queue | `us-west2/vma-turns-staging` |

Each environment is split into an API service and a worker service. API
instances accept HTTP/SSE traffic but never execute queued Agent turns. Worker
instances expose a private OIDC turn endpoint plus health endpoints. Each worker
admits at most 20 in-flight turn requests and keeps one
best-effort sweeper for expired Session leases while an instance is active.
Both roles keep CPU allocated, use one web process, and run the
same startup and database liveness probes. Each instance is pinned to one vCPU
and 4 GiB memory; API instances accept 80 concurrent HTTP requests while worker
instances use `containerConcurrency=20`.

Production and staging both scale API and worker services from zero. Only the
API services disable the Cloud Run Invoker IAM check; the worker services stay
private. Cloud Tasks push requests drive worker autoscaling, while PostgreSQL
Session leases remain the source of truth if a worker disappears. API request
autoscaling and Agent-turn capacity are independent.

## Prerequisites

- Install and authenticate the Google Cloud CLI.
- Ensure your account can enable APIs, edit IAM, submit builds, and manage Cloud
  Run, Cloud Tasks, Artifact Registry, Secret Manager, and Cloud Build triggers.
- Use managed PostgreSQL. SQLite is not durable or multi-instance safe on Cloud
  Run.
- Keep development, staging, and production in three separate Supabase projects;
  these environments must never share a database. Runtime SQLAlchemy traffic
  uses the Supavisor transaction pooler on port `6543`. LangGraph checkpoints
  and the event listener use session-affine URLs on port `5432`; the migration
  Job receives the same environment's direct secret. A transaction-mode pooler
  cannot preserve the checkpoint schema's session setting or a lifetime
  `LISTEN` connection. The local `.env` uses the development project; the two Secret
  Manager files below use staging and production.
- Build the operator-owned `vma-hardened` template in the E2B account before
  creating an E2B-backed session.

## One-time setup

Run the scripts in order:

```bash
./scripts/gcloud/0-setup-registry.sh
```

This enables the required APIs, creates one Artifact Registry in each runtime
region plus the dedicated runtime service account, and grants Cloud Build
permission to deploy as that identity. Runtime access to secrets is granted per
secret by the next step. Pass `staging` or `production` to repair only one
regional registry.

Create the Cloud Tasks queues and grant the runtime identity permission to
enqueue OIDC tasks:

```bash
./scripts/gcloud/8-setup-cloud-tasks.sh all
```

On the first run, the worker services do not exist yet, so the command configures
the queues, Enqueuer role, the Cloud Tasks primary service-agent role, and both
OIDC `iam.serviceAccounts.actAs` bindings, then reports that worker Invoker
bindings are pending. This is expected. The deploy
scripts create a missing worker once in `inline` bootstrap mode, query its real Cloud Run
URL, grant the runtime identity `roles/run.invoker` on that private service, and
only then render the final `cloud` worker and API revisions with that URL.
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
VMA_ENCRYPTION_KEY=
OPENROUTER_MANAGEMENT_KEY=
FIRECRAWL_API_KEY=
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
| `VMA_ENCRYPTION_KEY` | `vma-encryption-key` | `vma-encryption-key-staging` |
| `OPENROUTER_MANAGEMENT_KEY` | `vma-openrouter-management-key` | `vma-openrouter-management-key-staging` |
| `FIRECRAWL_API_KEY` | `vma-firecrawl-api-key` | `vma-firecrawl-api-key-staging` |
| `E2B_API_KEY` | `vma-e2b-api-key` | `vma-e2b-api-key-staging` |
| `S3_ENDPOINT_URL` | `vma-s3-endpoint-url` | `vma-s3-endpoint-url-staging` |
| `S3_ACCESS_KEY_ID` | `vma-s3-access-key-id` | `vma-s3-access-key-id-staging` |
| `S3_SECRET_ACCESS_KEY` | `vma-s3-secret-access-key` | `vma-s3-secret-access-key-staging` |
| `S3_BUCKET_NAME` | `vma-s3-bucket-name` | `vma-s3-bucket-name-staging` |

The API and worker manifests set `VMA_CHECKPOINT_DATABASE_URL` from the
port-5432 session/direct secret. LangGraph uses its own session-affine psycopg
connection and explicitly sets `search_path` after connecting; Supabase's
pooler discards the equivalent startup option. `VMA_LISTEN_DATABASE_URL` also
uses the session-mode secret, while ordinary application SQL continues to use
the transaction-pooler `DATABASE_URL`. The migration Job maps
`vma-database-url-direct[-staging]` to `DATABASE_URL` and initializes both the
Alembic schema and LangGraph checkpoint schema before traffic changes.

The Supabase URL and publishable key enable hosted owner and superadmin JWT
authentication. They must match the Votrix web application in each environment;
never substitute the Supabase service-role key.

The OpenRouter management credential creates and administers each Account's
inference key. It cannot perform inference itself and is separate from the
encrypted Account credentials used for model requests.

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
VMA_DB_POOL_SIZE=16
VMA_DB_MAX_OVERFLOW=8
```

Worker-specific settings are:

```env
VMA_RUN_SWEEPER=true
VMA_CHECKPOINT_POOL_MAX_SIZE=6
VMA_DB_POOL_SIZE=8
VMA_DB_MAX_OVERFLOW=4
```

Shared hosted settings include:

```env
TURN_DISPATCH=cloud
TASKS_PROJECT=votrixai-480422
TASKS_QUEUE=vma-turns[-staging]
TASKS_LOCATION=us-east4 (production) / us-west2 (staging)
TASKS_SERVICE_ACCOUNT=vma-runtime@votrixai-480422.iam.gserviceaccount.com
WORKER_URL=<discovered private Cloud Run worker URL>
VMA_DB_POOL_TIMEOUT_SECONDS=10
VMA_DB_POOL_RECYCLE_SECONDS=300
VMA_REQUESTS_PER_MINUTE=600
VMA_MAX_ACTIVE_WORK=20
VMA_ORGANIZATION_STORAGE_BYTES=5368709120
VMA_PUBLIC_GA_ONLY=true
VMA_CORS_ORIGINS=https://<matching-vma-developer-app>,https://docs.vma.votrixai.com
```

E2B turns resume from the sealed filesystem and do not rehydrate all inputs
from R2. Runtime SQLAlchemy uses transaction mode. Checkpoint calls borrow from
their own bounded pool, while each API/worker process keeps one session-mode
`LISTEN` connection for event wake-ups. PostgreSQL notifications are
best-effort; SSE clients reconcile missed notifications against durable Session
events. Each worker admits 20 turn requests, starts at zero instances, and may
scale to four only within the measured Supabase, E2B, provider, and spend
budgets in the scaling runbook.

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
clean commit image. The production script defaults to `us-east4`; staging
defaults to `us-west2`. Either script also accepts a legacy positional region
or `--region=REGION` for an intentional temporary comparison. The rendered
Cloud Tasks location always tracks that override, so create the matching
regional queue first.

Both scripts enforce the same sequence:

1. Build and push a commit-tagged image.
2. Deploy or update `<service>-migrate` with that exact image.
3. execute `sh scripts/migrate.sh` as a Cloud Run Job and wait for success.
4. If the worker does not exist, create it in `inline` mode and query the URL that
   Cloud Run actually assigned.
5. Grant the runtime OIDC identity `roles/run.invoker` on the private worker.
6. Render and replace the private worker in `cloud` mode with that URL.
7. Render and replace the API service in `cloud` mode only after the worker is
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
Cloud Run concurrency is the process bound:

```text
turn execution capacity = worker instances × containerConcurrency
```

The checked-in manifests use `containerConcurrency=20`, scale workers from zero,
and permit at most four instances in each environment, for up to 80 concurrent
turn requests after scale-out. The production maximum is not permission to deploy blindly:
the first production release remains blocked until the Supabase compute tier,
pooler client ceiling, transaction-pool backend budget, and operational
headroom are recorded in the scaling runbook.

Cloud Tasks delivery wakes a worker from zero and retries failed deliveries.
The expired-Session sweeper is not a replacement for successful task creation;
changing `minScale` does not repair queue or IAM failures. Queue retry policy is
`maxAttempts=8`, 5–300 second backoff, and at most 25 concurrent dispatches.
Each task sets its own 1,800-second dispatch deadline in application code; there
is deliberately no queue-level deadline setting.

Before raising `maxScale` or the per-instance turn limit, recalculate PostgreSQL,
model-provider, E2B, CPU, memory, and spend budgets. A manual Cloud Run scaling
change is temporary because the next manifest replacement restores the
checked-in bounds. Validate changes in staging and update the manifest, static
tests, and runbook together.

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
If an environment already has a legacy `vma_*` operator secret, the trusted
bootstrap imports it only when that Organization has no API-key rows; later
runs must match the existing active management key. The bootstrap uses the
same environment-specific database schema as the deploy scripts; override it
only during a coordinated schema rename with
`VMA_BOOTSTRAP_DATABASE_SCHEMA`.

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

Pass `staging` or `production` to repair only that environment's regional API.

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
and concurrent-dispatch bound from its configured region. Pass `staging` or
`production` to inspect one environment.

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
| `scripts/gcloud/8-setup-cloud-tasks.sh` | Idempotently configure queues and OIDC dispatch IAM |
| `scripts/gcloud/preflight.sh` | Read-only GCP, IAM, secret metadata, manifest, and git readiness |
| `scripts/gcloud/status.sh` | Deployed service and job status |
