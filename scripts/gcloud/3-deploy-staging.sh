#!/bin/sh
# Build, migrate, and deploy the staging API and worker Cloud Run services.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

usage() {
  echo "Usage: $0 [region|--region=REGION] [--allow-dirty]" >&2
}

REGION_OVERRIDE=""
ALLOW_DIRTY=false
for arg in "$@"; do
  case "$arg" in
    --region=*)
      REGION_OVERRIDE=${arg#--region=}
      if [ -z "$REGION_OVERRIDE" ]; then
        usage
        exit 2
      fi
      ;;
    --allow-dirty)
      ALLOW_DIRTY=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $arg" >&2
      usage
      exit 2
      ;;
    *)
      if [ -n "$REGION_OVERRIDE" ]; then
        echo "Only one region may be specified." >&2
        usage
        exit 2
      fi
      REGION_OVERRIDE=$arg
      ;;
  esac
done

REGION="${REGION_OVERRIDE:-$REGION}"
API_MANIFEST="${REPO_ROOT}/service.staging.yaml"
WORKER_MANIFEST="${REPO_ROOT}/service.worker.staging.yaml"
MIGRATION_JOB="${STAGING_SERVICE}-migrate"
DATABASE_SECRET="vma-database-url-direct-staging"

if ! git -C "$REPO_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "Staging deploys must run from a git checkout with a commit." >&2
  exit 1
fi

COMMIT_TAG=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
WORKTREE_STATUS=$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal)
if [ -n "$WORKTREE_STATUS" ] && [ "$ALLOW_DIRTY" != "true" ]; then
  echo "Staging deploys require a clean git worktree by default." >&2
  echo "Commit the changes, or retry explicitly with --allow-dirty." >&2
  git -C "$REPO_ROOT" status --short >&2
  exit 1
fi

if [ -n "$WORKTREE_STATUS" ]; then
  TAG="${COMMIT_TAG}-dirty-$(date -u +%Y%m%d%H%M%S)-$$"
  echo "WARNING: building dirty staging worktree as unique tag ${TAG}." >&2
else
  TAG=$COMMIT_TAG
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

WORKER_URL=$(gcloud run services describe "$STAGING_WORKER_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || true)

if [ -z "$WORKER_URL" ]; then
  echo "Creating ${STAGING_WORKER_SERVICE} in bootstrap poll mode..."
  sed \
    -e "s|IMAGE_URL|${IMAGE}|" \
    -e 's|value: "__VMA_WORKER_URL__"|value: ""|' \
    -e 's|value: "hybrid"|value: "poll"|' \
    "$WORKER_MANIFEST" | \
    gcloud run services replace \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      /dev/stdin \
      --quiet

  WORKER_URL=$(gcloud run services describe "$STAGING_WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.url)')
fi

if [ -z "$WORKER_URL" ]; then
  echo "Cloud Run did not report a worker service URL." >&2
  exit 1
fi

echo "Allowing Cloud Tasks to invoke ${STAGING_WORKER_SERVICE}..."
gcloud run services add-iam-policy-binding "$STAGING_WORKER_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet

echo "Deploying ${STAGING_WORKER_SERVICE} worker service in hybrid mode..."
sed \
  -e "s|IMAGE_URL|${IMAGE}|" \
  -e "s|__VMA_WORKER_URL__|${WORKER_URL}|" \
  "$WORKER_MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Deploying ${STAGING_SERVICE} API service in hybrid mode..."
sed \
  -e "s|IMAGE_URL|${IMAGE}|" \
  -e "s|__VMA_WORKER_URL__|${WORKER_URL}|" \
  "$API_MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Staging API and worker deployed: ${IMAGE}"
