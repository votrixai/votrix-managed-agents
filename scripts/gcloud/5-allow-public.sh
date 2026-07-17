#!/bin/sh
# Make both APIs reachable publicly by disabling the Cloud Run Invoker IAM
# check. This is the same mechanism declared in the service manifests and is
# Google's recommended option for public Cloud Run services. Database-backed
# tenant API-key auth still applies at the application layer.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"

gcloud run services update "$PRODUCTION_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --no-invoker-iam-check \
  --quiet

gcloud run services update "$STAGING_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --no-invoker-iam-check \
  --quiet

echo "Both VMA services have the Invoker IAM check disabled; database-backed tenant API-key auth remains enabled."
