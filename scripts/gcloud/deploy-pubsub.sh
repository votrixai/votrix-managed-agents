#!/bin/sh
# Called after migrations by the local deploy scripts or Cloud Build.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"
if [ "$#" -ne 5 ]; then
  echo "Usage: $0 APP_ENV REGION IMAGE BUILD_ID COMMIT_SHA" >&2
  exit 2
fi
APP_ENV=$1; REGION=$2; IMAGE=$3; BUILD_ID=$4; COMMIT_SHA=$5
case "$APP_ENV" in
  staging) SERVICE_NAME=$STAGING_SERVICE; SUFFIX=-staging ;;
  production) SERVICE_NAME=$PRODUCTION_SERVICE; SUFFIX="" ;;
  *) echo "Invalid APP_ENV: $APP_ENV" >&2; exit 2 ;;
esac
POOL_NAME="${SERVICE_NAME}-pool"
SUBSCRIPTION="vma-turns-worker${SUFFIX}"
PUSH_ENDPOINT=$(gcloud pubsub subscriptions describe "$SUBSCRIPTION" \
  --project="$PROJECT_ID" --format='value(pushConfig.pushEndpoint)')
if [ -n "$PUSH_ENDPOINT" ]; then
  echo "${SUBSCRIPTION} must be a Pull subscription." >&2
  exit 1
fi
# Preserve an operator's manual scale setting across revision deployments.
# Distinguish an absent pool from a failed read: a permission or network error
# must not silently reset an existing pool to one instance.
POOL_JSON=$(gcloud run worker-pools list --project="$PROJECT_ID" --region="$REGION" \
  --filter="metadata.name=${POOL_NAME}" --format=json)
INSTANCE_COUNT=$(printf '%s' "$POOL_JSON" | python3 -c '
import json, sys
pools = json.load(sys.stdin)
p = pools[0] if pools else {}
print(p.get("scaling", {}).get("manualInstanceCount",
      p.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/manualInstanceCount", 1)))
')
sed \
  -e "s|IMAGE_URL|${IMAGE}|g" \
  -e "s|__SERVICE_NAME__|${SERVICE_NAME}|g" \
  -e "s|__PROJECT_ID__|${PROJECT_ID}|g" \
  -e "s|__APP_ENV__|${APP_ENV}|g" \
  -e "s|__REGION__|${REGION}|g" \
  -e "s|__SECRET_SUFFIX__|${SUFFIX}|g" \
  -e "s|__INSTANCE_COUNT__|${INSTANCE_COUNT}|g" \
  -e "s|__VMA_PUBLIC_BUILD_ID__|${BUILD_ID}|g" \
  -e "s|__VMA_GIT_COMMIT_SHA__|${COMMIT_SHA}|g" \
  "${REPO_ROOT}/worker-pool.yaml" | \
  gcloud run worker-pools replace /dev/stdin --project="$PROJECT_ID" --quiet

# The cloud deployment path resolves its own worker URL on rollback.
sed \
  -e "s|IMAGE_URL|${IMAGE}|g" \
  -e "s|__VMA_PUBLIC_BUILD_ID__|${BUILD_ID}|g" \
  -e "s|__VMA_GIT_COMMIT_SHA__|${COMMIT_SHA}|g" \
  -e "s|__VMA_TASKS_LOCATION__|${REGION}|g" \
  -e 's|__VMA_WORKER_URL__||g' \
  "${REPO_ROOT}/service.${APP_ENV}.yaml" | \
  gcloud run services replace /dev/stdin --project="$PROJECT_ID" --region="$REGION" --quiet
