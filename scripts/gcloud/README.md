# Google Cloud Run deployment

This is the canonical deployment path for Votrix Managed Agents. It mirrors the
`votrix-backend` production/staging workflow while using a dedicated VMA runtime
identity and a migration gate before every service rollout.

## Fixed layout

| Setting | Value |
|---|---|
| Project | `votrixai-480422` |
| Region | `us-central1` |
| Artifact Registry | `votrix` |
| Production service | `votrix-managed-agents` |
| Staging service | `votrix-managed-agents-staging` |
| Runtime service account | `vma-runtime@votrixai-480422.iam.gserviceaccount.com` |

Production and staging each run exactly one warm instance. Both revisions use
one web worker plus five embedded durable-work consumers, keep CPU allocated,
expose only the public-GA API surface, allow browser calls only from
`https://docs.votrixai.com`, and run the same startup and database liveness
probes. Each instance is pinned to one vCPU, 4 GiB memory, and 40 concurrent
HTTP requests; this is a vertical public-beta baseline, not horizontal scale.

## Prerequisites

- Install and authenticate the Google Cloud CLI.
- Ensure your account can enable APIs, edit IAM, submit builds, and manage Cloud
  Run, Artifact Registry, Secret Manager, and Cloud Build triggers.
- Use managed PostgreSQL. SQLite is not durable or multi-instance safe on Cloud
  Run.
- Keep development, staging, and production in three separate Supabase projects;
  these environments must never share a database. Start with the Supavisor
  session-mode endpoint on port `5432` and use its SQLAlchemy `asyncpg` URL for
  `DATABASE_URL`. VMA derives LangGraph's `postgresql://` checkpoint DSN from
  that value, so the standard deployment does not duplicate the connection
  string or password. The local `.env` uses the development project; the two
  Secret Manager files below use staging and production.
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

Create the two untracked Secret Manager input files from the checked-in
templates, then replace every placeholder:

```bash
cp .env.production.example .env.production
cp .env.staging.example .env.staging
```

Each unquoted `KEY=value` file contains exactly these required values:

```env
DATABASE_URL=
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
| `VMA_ENCRYPTION_KEY` | `vma-encryption-key` | `vma-encryption-key-staging` |
| `E2B_API_KEY` | `vma-e2b-api-key` | `vma-e2b-api-key-staging` |
| `S3_ENDPOINT_URL` | `vma-s3-endpoint-url` | `vma-s3-endpoint-url-staging` |
| `S3_ACCESS_KEY_ID` | `vma-s3-access-key-id` | `vma-s3-access-key-id-staging` |
| `S3_SECRET_ACCESS_KEY` | `vma-s3-secret-access-key` | `vma-s3-secret-access-key-staging` |
| `S3_BUCKET_NAME` | `vma-s3-bucket-name` | `vma-s3-bucket-name-staging` |

`VMA_CHECKPOINT_DATABASE_URL` remains an optional application setting for the
unusual case where checkpoint tables intentionally live in another database.
It is not part of the standard Cloud Run Secret Manager contract.

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
loading them from Secret Manager:

```env
VMA_EMBEDDED_WORKER_ENABLED=true
VMA_WORKER_CONCURRENCY=5
VMA_WORKER_POLL_INTERVAL_SECONDS=0.5
VMA_WORKER_LEASE_SECONDS=120
VMA_EVENT_POLL_INTERVAL_SECONDS=1.0
VMA_MAX_SESSION_INPUT_BYTES=67108864
VMA_DB_POOL_SIZE=10
VMA_DB_MAX_OVERFLOW=5
VMA_DB_POOL_TIMEOUT_SECONDS=10
VMA_DB_POOL_RECYCLE_SECONDS=300
VMA_REQUESTS_PER_MINUTE=600
VMA_MAX_ACTIVE_WORK=20
VMA_ORGANIZATION_STORAGE_BYTES=5368709120
VMA_PUBLIC_GA_ONLY=true
VMA_CORS_ORIGINS=https://docs.votrixai.com
```

The 64 MiB aggregate Session-input cap bounds create-time materialization and
one-time E2B injection. E2B turns resume from the sealed filesystem and do not
rehydrate all inputs from R2. The PostgreSQL pool reuses connections for API,
SSE, and embedded-worker traffic instead of opening a new database connection
for every poll. Keep the combined pool ceiling within the selected Supabase
plan's connection limit.
The hosted Organization defaults admit bursts of up to 20 queued/running turns;
five execute concurrently and the durable queue absorbs the remainder.

## Manual deploys

```bash
./scripts/gcloud/2-deploy-production.sh
./scripts/gcloud/3-deploy-staging.sh
```

Both scripts enforce the same sequence:

1. Build and push a commit-tagged image.
2. Deploy or update `<service>-migrate` with that exact image.
3. execute `sh scripts/migrate.sh` as a Cloud Run Job and wait for success.
4. Replace the Cloud Run service only after migrations succeed.

The web entrypoint has no migration branch, so restarts never race to run
Alembic themselves.

## Staging acceptance gates

After staging deploys, run the controlled one-Session acceptance first:

```bash
VMA_SMOKE_BASE_URL=https://YOUR-STAGING-CLOUD-RUN-URL \
VMA_SMOKE_API_KEY=... \
uv run --extra sandbox-e2b python scripts/pilot_acceptance.py
```

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

## Bootstrap the first tenant API key

Authentication is database-backed in every environment. After the first
successful migration, run the bootstrap CLI once from a trusted operator
machine using the matching untracked environment file:

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

Connect the GitHub repository under **Cloud Build > Triggers**, then run:

```bash
./scripts/gcloud/4-setup-triggers.sh <github-owner> <repo-name>
```

This creates:

- `vma-deploy-production`: `main` → production
- `vma-deploy-staging`: `beta` → staging

Cloud Build uses the same manifests and the same blocking migration-job sequence
as manual deployment.

## Public API access

After both services exist:

```bash
./scripts/gcloud/5-allow-public.sh
```

This exposes the Cloud Run URLs. VMA still requires a database-backed Organization
API key; public ingress does not add an anonymous authentication path.

## Status

```bash
./scripts/gcloud/status.sh
```

The command prints each service URL, ready revision, immutable image tag, and
the image configured on each migration job.

## Files

| File | Purpose |
|---|---|
| `cloudbuild.yaml` | Build, push, migrate, then deploy |
| `service.production.yaml` | Production Cloud Run service |
| `service.staging.yaml` | Staging Cloud Run service |
| `scripts/gcloud/config.sh` | Shared project and service names |
| `scripts/gcloud/0-setup-registry.sh` | APIs, registry, runtime identity, IAM |
| `scripts/gcloud/1-create-secrets.sh` | Allowlisted Secret Manager import |
| `scripts/gcloud/2-deploy-production.sh` | Manual production rollout |
| `scripts/gcloud/3-deploy-staging.sh` | Manual staging rollout |
| `scripts/gcloud/4-setup-triggers.sh` | GitHub Cloud Build triggers |
| `scripts/gcloud/5-allow-public.sh` | Public Cloud Run invoker access |
| `scripts/gcloud/status.sh` | Deployed service and job status |
