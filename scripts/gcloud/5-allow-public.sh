#!/bin/sh
# Make both APIs reachable publicly. Database-backed tenant API-key auth still applies.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"

gcloud run services add-iam-policy-binding "$PRODUCTION_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --quiet

gcloud run services add-iam-policy-binding "$STAGING_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --quiet

echo "Both VMA services are publicly reachable; database-backed tenant API-key auth remains enabled."
