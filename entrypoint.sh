#!/bin/sh
set -eu

# Database migrations are a dedicated Cloud Run release Job, never a web
# cold-start side effect that could race across revisions or instances.
exec uvicorn app.server:create_app --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers "${WEB_CONCURRENCY:-1}"
