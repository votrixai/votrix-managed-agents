#!/bin/sh
# Build, migrate, and deploy the staging Cloud Run service.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"
MANIFEST="${REPO_ROOT}/service.staging.yaml"
MIGRATION_JOB="${STAGING_SERVICE}-migrate"
DATABASE_SECRET="vma-database-url-staging"

if git -C "$REPO_ROOT" rev-parse --short HEAD >/dev/null 2>&1; then
  TAG=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
else
  TAG=$(date -u +%Y%m%d%H%M%S)
fi
IMAGE="${REGISTRY}/${PROJECT_ID}/${REPOSITORY}/${STAGING_SERVICE}:${TAG}"

echo "Building and pushing ${IMAGE}..."
gcloud builds submit "$REPO_ROOT" \
  --project="$PROJECT_ID" \
  --tag="$IMAGE" \
  --quiet

echo "Deploying migration job ${MIGRATION_JOB}..."
gcloud run jobs deploy "$MIGRATION_JOB" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SERVICE_ACCOUNT" \
  --set-env-vars="APP_ENV=staging" \
  --set-secrets="DATABASE_URL=${DATABASE_SECRET}:latest" \
  --command=sh \
  --args=scripts/migrate.sh \
  --tasks=1 \
  --max-retries=0 \
  --task-timeout=900s \
  --cpu=1 \
  --memory=512Mi \
  --quiet

echo "Running migrations before changing staging traffic..."
gcloud run jobs execute "$MIGRATION_JOB" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --wait \
  --quiet

echo "Deploying ${STAGING_SERVICE}..."
sed "s|IMAGE_URL|${IMAGE}|" "$MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Staging deployed: ${IMAGE}"
