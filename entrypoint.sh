#!/bin/sh
set -eu

# Database migrations are a dedicated Cloud Run release Job, never a web
# cold-start side effect that could race across revisions or instances.
# Cloud Run terminates TLS at its front end and hands the container plain HTTP
# with the real scheme in `x-forwarded-proto`. uvicorn only trusts that header
# from 127.0.0.1, so without this it believes every request arrived over HTTP
# and builds absolute redirects pointing at `http://` and the internal
# `*.run.app` host — which a browser on an HTTPS page refuses to follow, and
# which leaks the origin address to any caller. Cloud Run admits no traffic
# except through that front end, so there is no less-trusting value available.
exec uvicorn app.server:create_app --factory \
  --host 0.0.0.0 \
  --port "${PORT:-8080}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --forwarded-allow-ips "*"
