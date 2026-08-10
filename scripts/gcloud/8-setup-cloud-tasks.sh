#!/bin/sh
# Create the Cloud Tasks queues and least-privilege dispatch IAM bindings.
# Run before the first deploy; rerun later only to repair queue or IAM drift.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
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

echo "Enabling the Cloud Tasks API..."
gcloud services enable cloudtasks.googleapis.com \
  --project="$PROJECT_ID" \
  --quiet

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')
CLOUD_TASKS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

echo "Ensuring the Cloud Tasks primary service agent exists..."
gcloud beta services identity create \
  --service=cloudtasks.googleapis.com \
  --project="$PROJECT_ID" \
  --quiet >/dev/null

echo "Pinning the Cloud Tasks service-agent role..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUD_TASKS_SERVICE_AGENT}" \
  --role="roles/cloudtasks.serviceAgent" \
  --quiet

echo "Allowing the VMA runtime identity to enqueue tasks..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/cloudtasks.enqueuer" \
  --quiet

echo "Allowing the runtime identity to attach its OIDC identity to tasks..."
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

echo "Allowing Cloud Tasks to mint OIDC tokens as the runtime identity..."
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${CLOUD_TASKS_SERVICE_AGENT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

ensure_queue() {
  QUEUE=$1
  QUEUE_LOCATION=$2

  if gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$QUEUE_LOCATION" >/dev/null 2>&1; then
    echo "Updating Cloud Tasks queue: ${QUEUE_LOCATION}/${QUEUE}"
    gcloud tasks queues update "$QUEUE" \
      --project="$PROJECT_ID" \
      --location="$QUEUE_LOCATION" \
      --max-attempts=8 \
      --min-backoff=5s \
      --max-backoff=300s \
      --max-concurrent-dispatches=25 \
      --quiet
  else
    echo "Creating Cloud Tasks queue: ${QUEUE_LOCATION}/${QUEUE}"
    gcloud tasks queues create "$QUEUE" \
      --project="$PROJECT_ID" \
      --location="$QUEUE_LOCATION" \
      --max-attempts=8 \
      --min-backoff=5s \
      --max-backoff=300s \
      --max-concurrent-dispatches=25 \
      --quiet
  fi

  QUEUE_STATE=$(gcloud tasks queues describe "$QUEUE" \
    --project="$PROJECT_ID" \
    --location="$QUEUE_LOCATION" \
    --format='value(state)')
  if [ "$QUEUE_STATE" = "PAUSED" ]; then
    echo "Resuming Cloud Tasks queue: ${QUEUE_LOCATION}/${QUEUE}"
    gcloud tasks queues resume "$QUEUE" \
      --project="$PROJECT_ID" \
      --location="$QUEUE_LOCATION" \
      --quiet
  fi
}

grant_worker_invoker() {
  WORKER_SERVICE=$1
  WORKER_REGION=$2

  if ! gcloud run services describe "$WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$WORKER_REGION" >/dev/null 2>&1; then
    echo "Worker service is not deployed yet: ${WORKER_SERVICE}" >&2
    echo "The deploy flow grants Invoker after bootstrap; rerun this setup only to repair drift." >&2
    return 0
  fi

  echo "Allowing OIDC task delivery to private worker: ${WORKER_SERVICE}"
  gcloud run services add-iam-policy-binding "$WORKER_SERVICE" \
    --project="$PROJECT_ID" \
    --region="$WORKER_REGION" \
    --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role="roles/run.invoker" \
    --quiet
}

setup_environment() {
  QUEUE=$1
  WORKER_SERVICE=$2
  ENVIRONMENT_REGION=$3
  ensure_queue "$QUEUE" "$ENVIRONMENT_REGION"
  grant_worker_invoker "$WORKER_SERVICE" "$ENVIRONMENT_REGION"
}

case "$TARGET" in
  production)
    setup_environment \
      "$PRODUCTION_TASKS_QUEUE" \
      "$PRODUCTION_WORKER_SERVICE" \
      "$PRODUCTION_REGION"
    ;;
  staging)
    setup_environment \
      "$STAGING_TASKS_QUEUE" \
      "$STAGING_WORKER_SERVICE" \
      "$STAGING_REGION"
    ;;
  all)
    setup_environment \
      "$PRODUCTION_TASKS_QUEUE" \
      "$PRODUCTION_WORKER_SERVICE" \
      "$PRODUCTION_REGION"
    setup_environment \
      "$STAGING_TASKS_QUEUE" \
      "$STAGING_WORKER_SERVICE" \
      "$STAGING_REGION"
    ;;
esac

echo "Cloud Tasks queues and dispatch IAM are configured."
