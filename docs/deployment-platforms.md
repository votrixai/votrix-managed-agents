# Deployment Platforms

This project follows a portable deployment shape:

- One application framework: FastAPI.
- One container contract: Docker/OCI image listening on `$PORT` or `8080`.
- One default process model: web and migration, with an optional worker for self-hosted queue execution.
- Platform-specific deployment files only under `deploy/`.

## Process Commands

| Process | Command | Notes |
| --- | --- | --- |
| Web | `scripts/start-web.sh` | Runs `uvicorn app.main:app` on `$PORT`. |
| Worker | `scripts/start-worker.sh` | Optional; runs `vma-worker` for queued `self_hosted` work. |
| Migration | `scripts/migrate.sh` | Runs `alembic upgrade head`; execute once per deploy. |

## Supported Targets

| Platform | Files | Fit |
| --- | --- | --- |
| Google Cloud Run | `deploy/gcp/` | Best managed-container default for GCP and small teams. |
| Render | `deploy/render/render.yaml` | Simple PaaS with Docker, optional workers, managed Postgres, and pre-deploy commands. |
| Railway | `deploy/railway/` | Fast MVP deployment with Docker and service-level start commands. |
| Fly.io | `deploy/fly/fly.toml` | Good for multi-region or long-running process deployments. |
| AWS ECS/Fargate | `deploy/aws/ecs-fargate/` | Enterprise AWS baseline; more infrastructure is required. |
| Docker Compose | `deploy/docker-compose/compose.yaml` | Local integration, simple VPS, and customer self-hosted smoke tests. |

## Portability Rules

- Keep provider SDKs out of request handlers unless they are behind a local adapter.
- Configure everything with environment variables.
- Store relational state behind `DATABASE_URL`.
- Store binary objects behind S3-compatible settings.
- Emit logs to stdout/stderr.
- Run migrations as release jobs, not as an implicit side effect of every web container start.
