---
title: Google Cloud Run Deployment
description: Supported Cloud Run topology, deployment workflow, and production constraints.
---

Google Cloud Run is the only maintained hosted deployment target for VMA. The repository does not ship or maintain Render, Railway, Fly.io, AWS ECS/Fargate, Kubernetes, or VPS deployment templates.

The deployment files intentionally mirror the surrounding Votrix backend layout:

```text
Dockerfile
cloudbuild.yaml
service.production.yaml
service.staging.yaml
service.worker.production.yaml
service.worker.staging.yaml
scripts/gcloud/
```

The operational walkthrough, one-time GCP setup, secret loading, manual deploys, CI/CD triggers, and status commands live in the [GCP operations guide](https://github.com/votrixai/votrix-managed-agents/tree/main/scripts/gcloud).

## Cloud Run API/worker topology

The maintained deployment separates public HTTP/SSE traffic from private Agent
turn execution:

```text
Cloud Run API (production min=1, max=3; WEB_CONCURRENCY=1)
    |-- HTTP/SSE and durable event replay
    |                  ^
    |                  `-- PostgreSQL NOTIFY previews
    v                                      |
VMA-owned PostgreSQL work/events ----> Cloud Run workers (min=1, max=8)
                                           |-- five concurrent turns per instance
                                           |-- model and MCP providers
                                           `-- external E2B sandboxes
```

Production allows one to three API instances and one to eight worker instances;
staging allows one to two of each. API instances scale from HTTP/SSE load.
Cloud Tasks sends one private push request per turn, allowing worker instances
to scale from queued work independently of API request load. A permanent
single-consumer PostgreSQL reconciler remains active in each worker instance so
a dispatch failure cannot lose durable work. Work attempts use unique lease IDs
and generations, heartbeat, recover after expiry, and fence stale terminal
writes.

Each instance uses one vCPU, 4 GiB memory, and one Uvicorn process. API instances
use `containerConcurrency=40` and a bounded 4+2 PostgreSQL application pool;
workers expose only private health and execution routes, use
`containerConcurrency=5`, a five-turn per-instance limiter, a 4+1 application
pool, and a three-connection checkpoint pool. The fallback reconciler has one
consumer and polls every 20 seconds. One-second event polling and a 64 MiB
aggregate Session-input cap remain shared. The cap limits create-time
materialization and one-time E2B injection; subsequent E2B turns use the
persisted seal and hydrate only files explicitly referenced by the current
model message.

Hosted services set `VMA_PREVIEW_BROKER=pg_notify`. Worker instances publish
coalesced typewriter/token and tool previews through PostgreSQL; each API process
holds one dedicated `LISTEN` connection and forwards received frames to local
SSE subscribers. The transport is best-effort and non-replayable, so clients
reconcile with durable events. Main and checkpoint traffic use the Supavisor
transaction-mode endpoint on port `6543`. Only `VMA_LISTEN_DATABASE_URL` uses
the session-mode endpoint on port `5432`, because transaction mode cannot
support the lifetime `LISTEN` connection or the janitor's session-scoped
advisory lock. Budget one additional database connection per API process, plus
the transient janitor lock connection. Local development retains
`process_local` as the default and may use one direct URL for every purpose.

The hosted Organization defaults admit 20 active queued/running turns and 600 API
requests per minute. Production starts with one warm worker and five warm turn
slots; Cloud Tasks can scale the worker service to eight instances. A larger
burst remains durable in the queue rather than failing at admission.
These values support an initial trusted-user rollout; they are not an
unlimited-throughput or exactly-once side-effect claim.

Cloud Run hosts the control plane and Deep Agents runtime. It does not host tenant shell sandboxes. With `VMA_SANDBOX_PROVIDER=e2b`, every cloud Session remains bound to its external E2B sandbox and reconnects that sandbox through the E2B API.

## Durable state

Production and staging must each use durable Postgres. Give VMA its own database
or, at minimum, its own schema and migration ownership; do not point it at the
schema owned by `votrix-backend`. Hosted `DATABASE_URL` and
`VMA_CHECKPOINT_DATABASE_URL` explicitly use the transaction pooler on port
`6543`; `VMA_LISTEN_DATABASE_URL` uses the session pooler on port `5432`.
Alembic runs in a migration Job whose `DATABASE_URL` comes from a separate
session/direct secret. Outside the hosted profile, the checkpoint and listener
settings may remain empty and fall back to `DATABASE_URL`.

Cloud Run's writable filesystem is ephemeral and must not hold authoritative
session, event, file, Skill, or checkpoint state. Configure a private
S3-compatible bucket for uploaded bytes and Skill archives. No public bucket
URL is required; authenticated VMA endpoints serve downloads. Public GA hides
the presign/complete upload routes.

Authentication is database-backed in local, development, staging, and
production, and fails closed until a trusted administrator runs
`python -m scripts.bootstrap_api_key` after migrations. The service enforces
request, active-work, daily model-token, and stored-byte limits and writes
append-only raw audit/usage facts. These are public-beta governance controls,
not Organization RBAC/SSO, Postgres RLS, priced billing, or enterprise audit
export/retention.

## Release flow

The supported release sequence is:

1. Cloud Build builds and pushes a commit-addressed image to Artifact Registry.
2. A dedicated Cloud Run migration Job runs `scripts/migrate.sh` against the target VMA database and must complete successfully.
3. Cloud Run replaces the API service with that image.
4. Cloud Run replaces the worker service with the same image only after the
   migration Job succeeds.
5. Traffic moves only after the service health checks pass.
6. Before promoting staging, run `scripts/pilot_acceptance.py` against its public
   URL with staging credentials. This provisions and deletes one real E2B
   Session while verifying Postgres, R2, model execution, append-only files,
   pause/resume, generated outputs, scoped listing, and download.
6. Run `scripts/performance_smoke.py` with ten independent Sessions and retain
   its latency/failure summary with the release evidence.

```bash
VMA_SMOKE_BASE_URL=https://staging-api.vma.votrixai.com \
VMA_SMOKE_API_KEY=... \
VMA_SMOKE_VAULT_IDS=vault_... \
uv run --extra sandbox-e2b python scripts/pilot_acceptance.py
```

Then run the concurrent gate with a staging Vault that already contains the
model Credential:

```bash
VMA_PERF_BASE_URL=https://staging-api.vma.votrixai.com \
VMA_PERF_API_KEY=... \
VMA_PERF_VAULT_IDS=vault_... \
uv run python scripts/performance_smoke.py
```

Migrations are a once-per-release operation. They are not an implicit side effect of every web container start.

## Optional external workers and schedules

`scripts/start-worker.sh` and the `self_hosted` Environment work protocol remain supported product capabilities. Here, `self_hosted` describes where an Agent Session's work executes; it does not describe where the VMA control plane is deployed. The GCP-only hosted decision therefore does not remove self-hosted Environment APIs or worker behavior.

The hosted API manifests explicitly disable embedded workers. The dedicated
worker-service manifests enable five embedded durable consumers for queued
Session execution and expose only private health routes. Scheduled Deployment
resources remain outside the public GA surface; no production scheduler invokes
their due-schedule tick.

## Process commands

| Process | Command | Cloud Run use |
| --- | --- | --- |
| API/worker Uvicorn process | `entrypoint.sh` (`scripts/start-web.sh` is a wrapper) | Both hosted services; `VMA_SERVICE_ROLE` selects the API or worker surface. Keep one process per instance. |
| Migration | `scripts/migrate.sh` | Dedicated release migration Job. |
| External worker CLI | `scripts/start-worker.sh` | Optional consumer for `self_hosted` Environment work; this is not the hosted worker-role service or an E2B sandbox. |
