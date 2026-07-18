#!/bin/sh
# Read-only deployment readiness checks. Secret values are never accessed.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

usage() {
  echo "Usage: $0 [all|staging|production] [--allow-dirty]" >&2
}

TARGET=all
ALLOW_DIRTY=false
for arg in "$@"; do
  case "$arg" in
    all|staging|production)
      TARGET=$arg
      ;;
    --allow-dirty)
      ALLOW_DIRTY=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      usage
      exit 2
      ;;
  esac
done

if [ "$ALLOW_DIRTY" = true ] && [ "$TARGET" != staging ]; then
  echo "--allow-dirty is supported only for a staging preflight." >&2
  exit 2
fi

FAILURES=0
WARNINGS=0

ok() {
  echo "[ok] $*"
}

fail() {
  echo "[fail] $*" >&2
  FAILURES=$((FAILURES + 1))
}

warn() {
  echo "[warn] $*" >&2
  WARNINGS=$((WARNINGS + 1))
}

has_line() {
  HAYSTACK=$1
  NEEDLE=$2
  while IFS= read -r line; do
    [ "$line" = "$NEEDLE" ] && return 0
  done <<EOF
$HAYSTACK
EOF
  return 1
}

check_manifest() {
  MANIFEST=$1
  PLACEHOLDER_IMAGE="${REGISTRY}/${PROJECT_ID}/${REPOSITORY}/vma-preflight:configuration-only"
  if sed "s|IMAGE_URL|${PLACEHOLDER_IMAGE}|" "${REPO_ROOT}/${MANIFEST}" | \
    gcloud run services replace \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --dry-run \
      /dev/stdin \
      --quiet >/dev/null 2>&1; then
    ok "Cloud Run manifest validates: ${MANIFEST}"
  else
    fail "Cloud Run manifest validation failed: ${MANIFEST}"
  fi
}

check_worker_manifest_is_private() {
  MANIFEST=$1
  if grep -q "run.googleapis.com/invoker-iam-disabled" "${REPO_ROOT}/${MANIFEST}"; then
    fail "worker manifest disables the Invoker IAM check: ${MANIFEST}"
  else
    ok "worker manifest keeps the Invoker IAM check enabled: ${MANIFEST}"
  fi
}

check_production_connection_gate() {
  if grep -q 'Status: UNMEASURED' "${REPO_ROOT}/private-docs/scaling-runbook.md"; then
    fail "production Supabase connection budget is UNMEASURED"
  else
    ok "production Supabase connection budget is recorded"
  fi
}

if ! command -v gcloud >/dev/null 2>&1; then
  fail "gcloud CLI is not installed"
  echo "Preflight failed: ${FAILURES} failure(s), ${WARNINGS} warning(s)." >&2
  exit 1
fi

ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1)
if [ -n "$ACCOUNT" ]; then
  ok "active gcloud account: ${ACCOUNT}"
else
  fail "no active gcloud account"
fi

if gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1; then
  ok "project is accessible: ${PROJECT_ID}"
else
  fail "project is not accessible: ${PROJECT_ID}"
fi

for api in \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  cloudtasks.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com
do
  ENABLED=$(gcloud services list \
    --project="$PROJECT_ID" \
    --enabled \
    --filter="config.name=${api}" \
    --format='value(config.name)' 2>/dev/null)
  if [ "$ENABLED" = "$api" ]; then
    ok "API enabled: ${api}"
  else
    fail "API not enabled: ${api}"
  fi
done

REGISTRY_FORMAT=$(gcloud artifacts repositories describe "$REPOSITORY" \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --format='value(format)' 2>/dev/null || true)
if [ "$REGISTRY_FORMAT" = DOCKER ]; then
  ok "Docker Artifact Registry exists: ${REGION}/${REPOSITORY}"
elif [ -n "$REGISTRY_FORMAT" ]; then
  fail "Artifact Registry is not Docker format: ${REGION}/${REPOSITORY} (${REGISTRY_FORMAT})"
else
  fail "Artifact Registry is missing: ${REGION}/${REPOSITORY}"
fi

if gcloud iam service-accounts describe "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  RUNTIME_DISABLED=$(gcloud iam service-accounts describe "$RUNTIME_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
    --format='value(disabled)' 2>/dev/null || true)
  if [ "$RUNTIME_DISABLED" = True ] || [ "$RUNTIME_DISABLED" = true ]; then
    fail "runtime service account is disabled: ${RUNTIME_SERVICE_ACCOUNT}"
  else
    ok "runtime service account exists and is enabled: ${RUNTIME_SERVICE_ACCOUNT}"
  fi
else
  fail "runtime service account is missing: ${RUNTIME_SERVICE_ACCOUNT}"
fi

BUILD_SERVICE_ACCOUNT=$(gcloud builds get-default-service-account \
  --project="$PROJECT_ID" 2>/dev/null || true)
if [ -z "$BUILD_SERVICE_ACCOUNT" ]; then
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
    --format='value(projectNumber)' 2>/dev/null || true)
  BUILD_SERVICE_ACCOUNT="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
fi

if [ -n "$BUILD_SERVICE_ACCOUNT" ]; then
  BUILD_ROLES=$(gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.members=serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
    --format='value(bindings.role)' 2>/dev/null || true)
  for role in \
    roles/cloudbuild.builds.builder \
    roles/artifactregistry.writer \
    roles/run.admin
  do
    if has_line "$BUILD_ROLES" "$role"; then
      ok "Cloud Build has ${role}"
    else
      fail "Cloud Build lacks ${role}: ${BUILD_SERVICE_ACCOUNT}"
    fi
  done

  ACT_AS=$(gcloud iam service-accounts get-iam-policy "$RUNTIME_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
    --format='value(bindings.members)' 2>/dev/null || true)
  if [ "$ACT_AS" = "serviceAccount:${BUILD_SERVICE_ACCOUNT}" ]; then
    ok "Cloud Build may deploy as the VMA runtime identity"
  else
    fail "Cloud Build cannot act as ${RUNTIME_SERVICE_ACCOUNT}"
  fi
else
  fail "could not determine the Cloud Build service account"
fi

RUNTIME_PROJECT_ROLES=$(gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members=serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --format='value(bindings.role)' 2>/dev/null || true)
if has_line "$RUNTIME_PROJECT_ROLES" roles/cloudtasks.enqueuer; then
  ok "runtime may enqueue Cloud Tasks"
else
  fail "runtime lacks roles/cloudtasks.enqueuer"
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)' 2>/dev/null || true)
CLOUD_TASKS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
CLOUD_TASKS_AGENT_ROLES=$(gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members=serviceAccount:${CLOUD_TASKS_SERVICE_AGENT}" \
  --format='value(bindings.role)' 2>/dev/null || true)
if has_line "$CLOUD_TASKS_AGENT_ROLES" roles/cloudtasks.serviceAgent; then
  ok "Cloud Tasks primary service agent has roles/cloudtasks.serviceAgent"
else
  fail "Cloud Tasks primary service agent lacks roles/cloudtasks.serviceAgent"
fi

RUNTIME_ACT_AS=$(gcloud iam service-accounts get-iam-policy "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --format='value(bindings.members)' 2>/dev/null || true)
if [ "$RUNTIME_ACT_AS" = "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" ]; then
  ok "runtime may attach its OIDC identity to Cloud Tasks"
else
  fail "runtime lacks iam.serviceAccounts.actAs on ${RUNTIME_SERVICE_ACCOUNT}"
fi


CLOUD_TASKS_ACT_AS=$(gcloud iam service-accounts get-iam-policy "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.role=roles/iam.serviceAccountUser AND bindings.members=serviceAccount:${CLOUD_TASKS_SERVICE_AGENT}" \
  --format='value(bindings.members)' 2>/dev/null || true)
if [ "$CLOUD_TASKS_ACT_AS" = "serviceAccount:${CLOUD_TASKS_SERVICE_AGENT}" ]; then
  ok "Cloud Tasks primary service agent may mint OIDC tokens as the runtime identity"
else
  fail "Cloud Tasks primary service agent cannot act as ${RUNTIME_SERVICE_ACCOUNT}"
fi

check_tasks_environment() {
  QUEUE=$1
  WORKER_SERVICE=$2

  if ! gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" >/dev/null 2>&1; then
    fail "Cloud Tasks queue is missing: ${TASKS_LOCATION}/${QUEUE}"
    return
  fi

  QUEUE_STATE=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" \
    --format='value(state)' 2>/dev/null || true)
  QUEUE_MAX_ATTEMPTS=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" \
    --format='value(retryConfig.maxAttempts)' 2>/dev/null || true)
  QUEUE_MIN_BACKOFF=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" \
    --format='value(retryConfig.minBackoff)' 2>/dev/null || true)
  QUEUE_MAX_BACKOFF=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" \
    --format='value(retryConfig.maxBackoff)' 2>/dev/null || true)
  QUEUE_MAX_CONCURRENT=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$TASKS_LOCATION" \
    --format='value(rateLimits.maxConcurrentDispatches)' 2>/dev/null || true)

  if [ "$QUEUE_STATE" = RUNNING ] && \
    [ "$QUEUE_MAX_ATTEMPTS" = 8 ] && \
    [ "$QUEUE_MIN_BACKOFF" = 5s ] && \
    [ "$QUEUE_MAX_BACKOFF" = 300s ] && \
    [ "$QUEUE_MAX_CONCURRENT" = 25 ]; then
    ok "Cloud Tasks queue policy is pinned: ${TASKS_LOCATION}/${QUEUE}"
  else
    fail "Cloud Tasks queue policy drifted: ${TASKS_LOCATION}/${QUEUE}"
  fi

  if ! gcloud run services describe "$WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" >/dev/null 2>&1; then
    warn "worker is not deployed yet; rerun 8-setup-cloud-tasks.sh after bootstrap: ${WORKER_SERVICE}"
    return
  fi

  WORKER_INVOKER=$(gcloud run services get-iam-policy "$WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --flatten='bindings[].members' \
    --filter="bindings.role=roles/run.invoker AND bindings.members=serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --format='value(bindings.members)' 2>/dev/null || true)
  if [ "$WORKER_INVOKER" = "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" ]; then
    ok "Cloud Tasks OIDC identity may invoke worker: ${WORKER_SERVICE}"
  else
    fail "runtime lacks roles/run.invoker on worker: ${WORKER_SERVICE}"
  fi
}

check_secret() {
  SECRET_NAME=$1
  if ! gcloud secrets describe "$SECRET_NAME" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
    fail "secret is missing: ${SECRET_NAME}"
    return
  fi

  ENABLED_VERSION=$(gcloud secrets versions list "$SECRET_NAME" \
    --project="$PROJECT_ID" \
    --filter='state=ENABLED' \
    --limit=1 \
    --format='value(name)' 2>/dev/null || true)
  if [ -n "$ENABLED_VERSION" ]; then
    ok "secret has an enabled version: ${SECRET_NAME}"
  else
    fail "secret has no enabled version: ${SECRET_NAME}"
  fi

  ACCESSOR=$(gcloud secrets get-iam-policy "$SECRET_NAME" \
    --project="$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.role=roles/secretmanager.secretAccessor AND bindings.members=serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --format='value(bindings.members)' 2>/dev/null || true)
  if [ "$ACCESSOR" = "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" ]; then
    ok "runtime may access secret: ${SECRET_NAME}"
  else
    fail "runtime cannot access secret: ${SECRET_NAME}"
  fi
}

check_environment_secrets() {
  SECRET_SUFFIX=$1
  for base in \
    database-url \
    database-url-direct \
    listen-database-url \
    encryption-key \
    e2b-api-key \
    supabase-url \
    supabase-publishable-key \
    s3-endpoint-url \
    s3-access-key-id \
    s3-secret-access-key \
    s3-bucket-name
  do
    check_secret "vma-${base}${SECRET_SUFFIX}"
  done
}

case "$TARGET" in
  production)
    check_production_connection_gate
    check_tasks_environment "$PRODUCTION_TASKS_QUEUE" "$PRODUCTION_WORKER_SERVICE"
    check_environment_secrets ""
    check_manifest service.production.yaml
    check_manifest service.worker.production.yaml
    check_worker_manifest_is_private service.worker.production.yaml
    ;;
  staging)
    check_tasks_environment "$STAGING_TASKS_QUEUE" "$STAGING_WORKER_SERVICE"
    check_environment_secrets "-staging"
    check_manifest service.staging.yaml
    check_manifest service.worker.staging.yaml
    check_worker_manifest_is_private service.worker.staging.yaml
    ;;
  all)
    check_production_connection_gate
    check_tasks_environment "$PRODUCTION_TASKS_QUEUE" "$PRODUCTION_WORKER_SERVICE"
    check_tasks_environment "$STAGING_TASKS_QUEUE" "$STAGING_WORKER_SERVICE"
    check_environment_secrets ""
    check_environment_secrets "-staging"
    check_manifest service.production.yaml
    check_manifest service.worker.production.yaml
    check_manifest service.staging.yaml
    check_manifest service.worker.staging.yaml
    check_worker_manifest_is_private service.worker.production.yaml
    check_worker_manifest_is_private service.worker.staging.yaml
    ;;
esac

WORKTREE_STATUS=$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal 2>/dev/null || true)
if [ -z "$WORKTREE_STATUS" ]; then
  ok "git worktree is clean"
elif [ "$TARGET" = staging ] && [ "$ALLOW_DIRTY" = true ]; then
  warn "git worktree is dirty; only an explicit staging --allow-dirty deploy is safe"
else
  fail "git worktree is dirty; commit changes before deploying ${TARGET}"
fi

if [ "$TARGET" = staging ] || [ "$TARGET" = all ]; then
  if git -C "$REPO_ROOT" ls-remote --exit-code --heads origin staging >/dev/null 2>&1; then
    ok "remote staging branch exists"
  else
    warn "remote staging branch is missing; the automatic staging trigger cannot fire yet"
  fi
fi

if [ "$TARGET" = production ] || [ "$TARGET" = all ]; then
  if git -C "$REPO_ROOT" ls-remote --exit-code --heads origin main >/dev/null 2>&1; then
    ok "remote main branch exists"
  else
    fail "remote main branch is missing; the production trigger cannot fire"
  fi
fi

warn "verify the private E2B template 'vma-hardened' exists for both configured E2B credentials"
warn "preflight checks secret metadata only; the staging migration and acceptance smoke validate secret values, Postgres, R2, E2B, and the model Vault"

if [ "$FAILURES" -gt 0 ]; then
  echo "Preflight failed: ${FAILURES} failure(s), ${WARNINGS} warning(s)." >&2
  exit 1
fi

echo "Preflight passed with ${WARNINGS} warning(s)."
