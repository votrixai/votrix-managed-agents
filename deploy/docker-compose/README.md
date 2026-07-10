# Docker Compose

This target is for local integration, simple VPS deployments, and customer
self-hosted smoke tests. It uses Postgres plus MinIO as S3-compatible object
storage, and the same root Dockerfile and process scripts as the cloud targets.

Start Postgres and the web service:

```bash
docker compose -f deploy/docker-compose/compose.yaml up --build web
```

Run migrations once:

```bash
docker compose -f deploy/docker-compose/compose.yaml --profile migrate run --rm migrate
```

Run the worker too:

```bash
docker compose -f deploy/docker-compose/compose.yaml --profile worker up --build web worker
```

The compose file uses MinIO through the same `S3_*` settings as production.
MinIO is exposed on `http://localhost:9100`, and the MinIO console is exposed
on `http://localhost:9101`.
