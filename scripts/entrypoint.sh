#!/bin/sh
set -e

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  scripts/migrate.sh
fi

exec scripts/start-web.sh
