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
scripts/gcloud/
```

The operational walkthrough, one-time GCP setup, secret loading, manual deploys, CI/CD triggers, and status commands live in the [GCP operations guide](https://github.com/votrixai/votrix-managed-agents/tree/main/scripts/gcloud).

## Cloud Run MVP topology

The current production manifest is intentionally constrained to one Cloud Run instance and one Uvicorn worker:

```text
Cloud Run web/API + embedded durable worker (minScale=1, maxScale=1, WEB_CONCURRENCY=1)
    |-- VMA-owned Postgres database/schema
    |-- S3-compatible object storage
    |-- model and MCP providers
    `-- external E2B sandboxes
```

Both production and staging keep one warm revision because the embedded worker
polls durable work continuously. Work attempts use unique lease IDs and
generations, heartbeat, recover after expiry, and fence stale terminal writes.
Do not increase `WEB_CONCURRENCY` or `maxScale` until the remaining
per-Session/checkpoint ownership and cross-process preview delivery have been
validated under the same tenant-isolation contract. The single-instance shape
is an explicit public-beta rollout constraint, not an availability guarantee.

Cloud Run hosts the control plane and Deep Agents runtime. It does not host tenant shell sandboxes. With `VMA_SANDBOX_PROVIDER=e2b`, every cloud Session remains bound to its external E2B sandbox and reconnects that sandbox through the E2B API.

## Durable state

Production and staging must each use durable Postgres. Give VMA its own database or, at minimum, its own schema and migration ownership; do not point it at the schema owned by `votrix-backend`. `DATABASE_URL` supplies control-plane persistence and, by default, the derived LangGraph checkpoint connection. `VMA_CHECKPOINT_DATABASE_URL` is an optional override only when checkpoints should use a different database.

Cloud Run's writable filesystem is ephemeral and must not hold authoritative
session, event, file, Skill, or checkpoint state. Configure a private
S3-compatible bucket for uploaded bytes and Skill archives. No public bucket
URL is required; authenticated VMA endpoints serve downloads. Public GA hides
the presign/complete upload routes.

Hosted authentication is database-backed and fails closed until a trusted
administrator runs `python -m scripts.bootstrap_api_key` after migrations. The
service enforces request, active-work, daily model-token, and stored-byte
limits and writes append-only raw audit/usage facts. These are public-beta
governance controls, not Organization RBAC/SSO, Postgres RLS, priced billing,
or enterprise audit export/retention.

## Release flow

The supported release sequence is:

1. Cloud Build builds and pushes a commit-addressed image to Artifact Registry.
2. A dedicated Cloud Run migration Job runs `scripts/migrate.sh` against the target VMA database and must complete successfully.
3. Cloud Run applies the production or staging service manifest with the same image.
4. Traffic moves only after the service health checks pass.
5. Before promoting staging, run `scripts/pilot_acceptance.py` against its public
   URL with staging credentials. This provisions and deletes one real E2B
   Session while verifying Postgres, R2, model execution, append-only files,
   pause/resume, generated outputs, scoped listing, and download.

```bash
VMA_SMOKE_BASE_URL=https://staging-managed-agents.votrixai.com \
VMA_SMOKE_API_KEY=... \
uv run --extra sandbox-e2b python scripts/pilot_acceptance.py
```

Migrations are a once-per-release operation. They are not an implicit side effect of every web container start.

## Optional workers and schedules

`scripts/start-worker.sh` and the `self_hosted` Environment work protocol remain supported product capabilities. Here, `self_hosted` describes where an Agent Session's work executes; it does not describe where the VMA control plane is deployed. The GCP-only hosted decision therefore does not remove self-hosted Environment APIs or worker behavior.

The hosted manifests enable the embedded durable worker for queued Session
execution. Scheduled Deployment resources remain outside the public GA surface;
no production scheduler invokes their due-schedule tick.

## Process commands

| Process | Command | Cloud Run use |
| --- | --- | --- |
| Web | `scripts/start-web.sh` | Main Cloud Run service; keep one process. |
| Migration | `scripts/migrate.sh` | Dedicated release migration Job. |
| Worker | `scripts/start-worker.sh` | Optional consumer for `self_hosted` Environment work, not an E2B sandbox. |
