#!/bin/sh
set -e

exec uvicorn votrix_managed_agents:create_app --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers "${WEB_CONCURRENCY:-1}"
