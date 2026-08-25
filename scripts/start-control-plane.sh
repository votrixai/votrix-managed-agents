#!/bin/sh
set -eu

exec uvicorn app.control_plane:app \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --forwarded-allow-ips "*"
