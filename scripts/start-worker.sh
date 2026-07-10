#!/bin/sh
set -e

set -- --poll-interval "${WORKER_POLL_INTERVAL_SECONDS:-1}"

if [ -n "${WORKER_ENVIRONMENT_ID:-}" ]; then
  set -- "$@" --environment-id "${WORKER_ENVIRONMENT_ID}"
fi

exec vma-worker "$@"
