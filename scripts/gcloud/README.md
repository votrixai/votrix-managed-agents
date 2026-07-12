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

Production runs exactly one warm instance. Staging scales from zero to one.
Both revisions use one web worker, keep CPU allocated, allow one-hour requests,
and run the same startup and database liveness probes.

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
VMA_API_KEY=
VMA_ENCRYPTION_KEY=
OPENROUTER_API_KEY=
E2B_API_KEY=
S3_ENDPOINT_URL=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_BUCKET_NAME=
S3_PUBLIC_URL=
VMA_PUBLIC_BASE_URL=
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
| `VMA_API_KEY` | `vma-api-key` | `vma-api-key-staging` |
| `VMA_ENCRYPTION_KEY` | `vma-encryption-key` | `vma-encryption-key-staging` |
| `OPENROUTER_API_KEY` | `vma-openrouter-api-key` | `vma-openrouter-api-key-staging` |
| `E2B_API_KEY` | `vma-e2b-api-key` | `vma-e2b-api-key-staging` |
| `S3_ENDPOINT_URL` | `vma-s3-endpoint-url` | `vma-s3-endpoint-url-staging` |
| `S3_ACCESS_KEY_ID` | `vma-s3-access-key-id` | `vma-s3-access-key-id-staging` |
| `S3_SECRET_ACCESS_KEY` | `vma-s3-secret-access-key` | `vma-s3-secret-access-key-staging` |
| `S3_BUCKET_NAME` | `vma-s3-bucket-name` | `vma-s3-bucket-name-staging` |
| `S3_PUBLIC_URL` | `vma-s3-public-url` | `vma-s3-public-url-staging` |
| `VMA_PUBLIC_BASE_URL` | `vma-public-base-url` | `vma-public-base-url-staging` |

`VMA_CHECKPOINT_DATABASE_URL` remains an optional application setting for the
unusual case where checkpoint tables intentionally live in another database.
It is not part of the standard Cloud Run Secret Manager contract.

Do not quote values in these files, and do not commit them.

The platform-level default route uses the shared OpenRouter key and the static
latency-first Fireworks/Together provider policy. Anthropic, OpenAI, DeepSeek,
and operator-registered providers can instead resolve tenant-specific keys from
a Session-mounted VMA Vault; those tenant keys do not belong in these deployment
files.

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

This exposes the Cloud Run URLs. VMA still requires its application-level API
key; anonymous local access is explicitly disabled in both cloud manifests.

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
