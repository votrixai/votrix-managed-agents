#!/bin/sh
# Show deployed images, revisions, URLs, and migration jobs.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"

for service in "$PRODUCTION_SERVICE" "$STAGING_SERVICE"; do
  image=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(spec.template.spec.containers[0].image)" 2>/dev/null || true)
  if [ -z "$image" ]; then
    echo "[$service]"
    echo "  Status: not deployed"
    echo ""
    continue
  fi

  revision=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.latestReadyRevisionName)" 2>/dev/null || true)
  url=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null || true)
  tag=$(printf '%s' "$image" | sed 's/.*://')

  echo "[$service]"
  echo "  URL: $url"
  echo "  Revision: $revision"
  echo "  Image: $image"
  if git rev-parse --verify "$tag" >/dev/null 2>&1; then
    echo "  Commit: $(git log "$tag" --oneline -1)"
  else
    echo "  Commit/tag: $tag"
  fi
  echo ""
done

for job in "${PRODUCTION_SERVICE}-migrate" "${STAGING_SERVICE}-migrate"; do
  job_image=$(gcloud run jobs describe "$job" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(spec.template.spec.template.spec.containers[0].image)" 2>/dev/null || true)
  echo "[$job]"
  if [ -z "$job_image" ]; then
    echo "  Status: not deployed"
    echo ""
    continue
  fi
  echo "  Image: $job_image"
  echo ""
done

for trigger in vma-deploy-production vma-deploy-staging; do
  trigger_id=$(gcloud builds triggers describe "$trigger" \
    --project="$PROJECT_ID" \
    --region="${VMA_TRIGGER_REGION:-global}" \
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
