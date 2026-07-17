#!/bin/sh
# Make both API services reachable publicly by disabling the Cloud Run Invoker IAM
# check. This is the same mechanism declared in the service manifests and is
# Google's recommended option for public Cloud Run services. Database-backed
# tenant API-key auth still applies at the application layer.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"

allow_public_api() {
  SERVICE=$1
  case "$SERVICE" in
    *worker*)
      echo "Refusing to make a VMA worker service public: ${SERVICE}" >&2
      exit 1
      ;;
  esac

  gcloud run services update "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --no-invoker-iam-check \
    --quiet
}

allow_public_api "$PRODUCTION_SERVICE"
allow_public_api "$STAGING_SERVICE"

echo "Both VMA API services have the Invoker IAM check disabled."
echo "Worker services remain private; database-backed tenant API-key auth remains enabled."
