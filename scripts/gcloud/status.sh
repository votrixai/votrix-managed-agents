#!/bin/sh
# Show deployed images, revisions, URLs, and migration jobs.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

usage() {
  echo "Usage: $0 [all|staging|production]" >&2
}

TARGET=${1:-all}
case "$TARGET" in
  all|staging|production)
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage
    exit 2
    ;;
esac

show_service() {
  service=$1
  region=$2
  image=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$region" \
    --format="value(spec.template.spec.containers[0].image)" 2>/dev/null || true)
  if [ -z "$image" ]; then
    echo "[cloud-run/${region}/${service}]"
    echo "  Status: not deployed"
    echo ""
    return
  fi

  revision=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$region" \
    --format="value(status.latestReadyRevisionName)" 2>/dev/null || true)
  url=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$region" \
    --format="value(status.url)" 2>/dev/null || true)
  tag=$(printf '%s' "$image" | sed 's/.*://')

  echo "[cloud-run/${region}/${service}]"
  echo "  URL: $url"
  echo "  Revision: $revision"
  echo "  Image: $image"
  if git -C "$REPO_ROOT" rev-parse --verify "$tag" >/dev/null 2>&1; then
    echo "  Commit: $(git -C "$REPO_ROOT" log "$tag" --oneline -1)"
  else
    echo "  Commit/tag: $tag"
  fi
  echo ""
}

show_job() {
  job=$1
  region=$2
  job_image=$(gcloud run jobs describe "$job" \
    --project="$PROJECT_ID" \
    --region="$region" \
    --format="value(spec.template.spec.template.spec.containers[0].image)" 2>/dev/null || true)
  echo "[cloud-run-job/${region}/${job}]"
  if [ -z "$job_image" ]; then
    echo "  Status: not deployed"
    echo ""
    return
  fi
  echo "  Image: $job_image"
  echo ""
}

show_queue() {
  queue=$1
  location=$2
  queue_state=$(gcloud tasks queues describe "$queue" \
    --project="$PROJECT_ID" \
    --location="$location" \
    --format='value(state)' 2>/dev/null || true)
  echo "[cloud-tasks/${location}/${queue}]"
  if [ -z "$queue_state" ]; then
    echo "  Status: not configured"
    echo ""
    return
  fi

  max_attempts=$(gcloud tasks queues describe "$queue" \
    --project="$PROJECT_ID" \
    --location="$location" \
    --format='value(retryConfig.maxAttempts)' 2>/dev/null || true)
  max_concurrent=$(gcloud tasks queues describe "$queue" \
    --project="$PROJECT_ID" \
    --location="$location" \
    --format='value(rateLimits.maxConcurrentDispatches)' 2>/dev/null || true)
  echo "  Status: $queue_state"
  echo "  Max attempts: $max_attempts"
  echo "  Max concurrent dispatches: $max_concurrent"
  echo ""
}

show_environment() {
  api_service=$1
  worker_service=$2
  queue=$3
  region=$4
  show_service "$api_service" "$region"
  show_service "$worker_service" "$region"
  show_job "${api_service}-migrate" "$region"
  show_queue "$queue" "$region"
}

case "$TARGET" in
  production)
    show_environment \
      "$PRODUCTION_SERVICE" \
      "$PRODUCTION_WORKER_SERVICE" \
      "$PRODUCTION_TASKS_QUEUE" \
      "$PRODUCTION_REGION"
    ;;
  staging)
    show_environment \
      "$STAGING_SERVICE" \
      "$STAGING_WORKER_SERVICE" \
      "$STAGING_TASKS_QUEUE" \
      "$STAGING_REGION"
    ;;
  all)
    show_environment \
      "$PRODUCTION_SERVICE" \
      "$PRODUCTION_WORKER_SERVICE" \
      "$PRODUCTION_TASKS_QUEUE" \
      "$PRODUCTION_REGION"
    show_environment \
      "$STAGING_SERVICE" \
      "$STAGING_WORKER_SERVICE" \
      "$STAGING_TASKS_QUEUE" \
      "$STAGING_REGION"
    ;;
esac

for trigger in vma-deploy-production vma-deploy-staging; do
  trigger_id=$(gcloud builds triggers describe "$trigger" \
    --project="$PROJECT_ID" \
    --region="${VMA_TRIGGER_REGION:-$CLOUD_BUILD_REGION}" \
    --format="value(id)" 2>/dev/null || true)
  echo "[$trigger]"
  if [ -z "$trigger_id" ]; then
    echo "  Status: not configured"
  else
    echo "  Status: configured"
    echo "  ID: $trigger_id"
  fi
  echo ""
done
