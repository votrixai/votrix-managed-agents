#!/bin/sh
# Build, migrate, and deploy the production API and worker Cloud Run services.

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

REGION="${REGION_OVERRIDE:-$PRODUCTION_REGION}"
REGISTRY="${REGION}-docker.pkg.dev"
API_MANIFEST="${REPO_ROOT}/service.production.yaml"
WORKER_MANIFEST="${REPO_ROOT}/service.worker.production.yaml"
CONTROL_PLANE_MANIFEST="${REPO_ROOT}/service.control-plane.production.yaml"
MIGRATION_JOB="${PRODUCTION_SERVICE}-migrate"
DATABASE_SECRET="vma-database-url-direct"
DATABASE_SCHEMA="vma"

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
FULL_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
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
  --set-env-vars="APP_ENV=production,DATABASE_SCHEMA=${DATABASE_SCHEMA}" \
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

WORKER_URL=$(gcloud run services describe "$PRODUCTION_WORKER_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || true)

if [ -z "$WORKER_URL" ]; then
  echo "Creating ${PRODUCTION_WORKER_SERVICE} in bootstrap poll mode..."
  sed \
    -e "s|IMAGE_URL|${IMAGE}|" \
    -e "s|__VMA_PUBLIC_BUILD_ID__|${TAG}|" \
    -e "s|__VMA_GIT_COMMIT_SHA__|${FULL_COMMIT}|" \
    -e "s|__VMA_TASKS_LOCATION__|${REGION}|" \
    -e 's|value: "__VMA_WORKER_URL__"|value: ""|' \
    -e 's|value: "cloud"|value: "inline"|' \
    "$WORKER_MANIFEST" | \
    gcloud run services replace \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      /dev/stdin \
      --quiet

  WORKER_URL=$(gcloud run services describe "$PRODUCTION_WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.url)')
fi

if [ -z "$WORKER_URL" ]; then
  echo "Cloud Run did not report a worker service URL." >&2
  exit 1
fi

echo "Allowing Cloud Tasks to invoke ${PRODUCTION_WORKER_SERVICE}..."
gcloud run services add-iam-policy-binding "$PRODUCTION_WORKER_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet

echo "Deploying ${PRODUCTION_WORKER_SERVICE} worker service in hybrid mode..."
sed \
  -e "s|IMAGE_URL|${IMAGE}|" \
  -e "s|__VMA_PUBLIC_BUILD_ID__|${TAG}|" \
  -e "s|__VMA_GIT_COMMIT_SHA__|${FULL_COMMIT}|" \
  -e "s|__VMA_TASKS_LOCATION__|${REGION}|" \
  -e "s|__VMA_WORKER_URL__|${WORKER_URL}|" \
  "$WORKER_MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Deploying ${PRODUCTION_SERVICE} API service in hybrid mode..."
sed \
  -e "s|IMAGE_URL|${IMAGE}|" \
  -e "s|__VMA_PUBLIC_BUILD_ID__|${TAG}|" \
  -e "s|__VMA_GIT_COMMIT_SHA__|${FULL_COMMIT}|" \
  -e "s|__VMA_TASKS_LOCATION__|${REGION}|" \
  -e "s|__VMA_WORKER_URL__|${WORKER_URL}|" \
  "$API_MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Deploying private ${PRODUCTION_CONTROL_PLANE_SERVICE} service..."
sed \
  -e "s|IMAGE_URL|${IMAGE}|" \
  -e "s|__VMA_PUBLIC_BUILD_ID__|${TAG}|" \
  -e "s|__VMA_GIT_COMMIT_SHA__|${FULL_COMMIT}|" \
  "$CONTROL_PLANE_MANIFEST" | \
  gcloud run services replace \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    /dev/stdin \
    --quiet

echo "Allowing only the Developer App identity to invoke the control plane..."
gcloud run services add-iam-policy-binding "$PRODUCTION_CONTROL_PLANE_SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:${VERCEL_INVOKER_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" \
  --quiet

echo "Production API, worker, and private control plane deployed: ${IMAGE}"
