#!/bin/sh
# Build, migrate, and deploy the production Cloud Run service.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

usage() {
  echo "Usage: $0 [region|--region=REGION]" >&2
}

REGION_OVERRIDE=""
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
      echo "Production deploys never allow a dirty git worktree." >&2
      exit 2
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
MANIFEST="${REPO_ROOT}/service.production.yaml"
MIGRATION_JOB="${PRODUCTION_SERVICE}-migrate"
DATABASE_SECRET="vma-database-url"

if ! git -C "$REPO_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "Production deploys must run from a git checkout with a commit." >&2
  exit 1
fi

WORKTREE_STATUS=$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal)
if [ -n "$WORKTREE_STATUS" ]; then
  echo "Production deploys require a clean git worktree." >&2
  echo "Commit or remove tracked, staged, and untracked changes before retrying." >&2
  git -C "$REPO_ROOT" status --short >&2
  exit 1
fi

TAG=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
IMAGE="${REGISTRY}/${PROJECT_ID}/${REPOSITORY}/${PRODUCTION_SERVICE}:${TAG}"

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
  --set-env-vars="APP_ENV=production" \
  --set-secrets="DATABASE_URL=${DATABASE_SECRET}:latest" \
  --command=sh \
  --args=scripts/migrate.sh \
  --tasks=1 \
  --max-retries=0 \
  --task-timeout=900s \
  --cpu=1 \
  --memory=512Mi \
  --quiet

echo "Running migrations before changing production traffic..."
gcloud run jobs execute "$MIGRATION_JOB" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --wait \
  --quiet

echo "Deploying ${PRODUCTION_SERVICE}..."
sed "s|IMAGE_URL|${IMAGE}|" "$MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Production deployed: ${IMAGE}"
