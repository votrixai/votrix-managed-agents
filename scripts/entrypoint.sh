#!/bin/sh
set -eu

# Backward-compatible process name for existing local deployments. Migrations
# intentionally do not run on web startup; use scripts/migrate.sh separately.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec "${SCRIPT_DIR}/../entrypoint.sh"
