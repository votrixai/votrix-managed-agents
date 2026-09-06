#!/bin/sh
# One-time provisioning; normal deploys only update the pool and API.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"
case "${1:-}" in
  staging) SUFFIX=-staging; REGION=${2:-$STAGING_REGION} ;;
  production) SUFFIX=""; REGION=${2:-$PRODUCTION_REGION} ;;
  *) echo "Usage: $0 staging|production [REGION]" >&2; exit 2 ;;
esac
TOPIC="vma-turns${SUFFIX}"
SUBSCRIPTION="vma-turns-worker${SUFFIX}"
gcloud services enable pubsub.googleapis.com --project="$PROJECT_ID" --quiet
if ! gcloud pubsub topics describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud pubsub topics create "$TOPIC" --project="$PROJECT_ID" \
    --message-storage-policy-allowed-regions="$REGION" --quiet
fi
if ! gcloud pubsub subscriptions describe "$SUBSCRIPTION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "$SUBSCRIPTION" --project="$PROJECT_ID" \
    --topic="$TOPIC" --ack-deadline=60 --message-retention-duration=7d \
    --expiration-period=never --min-retry-delay=10s --max-retry-delay=60s --quiet
fi
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" --role=roles/pubsub.publisher --quiet
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" --role=roles/pubsub.subscriber --quiet
