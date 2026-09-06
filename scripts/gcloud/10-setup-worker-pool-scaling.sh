#!/bin/sh
# One-time control-plane setup; run before switching the API to Pub/Sub.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"
case "${1:-}" in
  staging) SERVICE_NAME=$STAGING_SERVICE; REGION=${2:-$STAGING_REGION}; SUFFIX=-staging ;;
  production) SERVICE_NAME=$PRODUCTION_SERVICE; REGION=${2:-$PRODUCTION_REGION}; SUFFIX="" ;;
  *) echo "Usage: $0 staging|production [REGION]" >&2; exit 2 ;;
esac
IMAGE=$(gcloud run services describe "$SERVICE_NAME" --project="$PROJECT_ID" \
  --region="$REGION" --format='value(spec.template.spec.containers[0].image)')
if [ -z "$IMAGE" ]; then echo "Deploy the compatibility image first." >&2; exit 1; fi
gcloud services enable cloudscheduler.googleapis.com --project="$PROJECT_ID" --quiet
gcloud beta services identity create --service=cloudscheduler.googleapis.com \
  --project="$PROJECT_ID" --quiet >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
  --role=roles/cloudscheduler.serviceAgent --quiet

# The Scheduler identity can only invoke the scaler; it cannot change pools or DBs.
ACCOUNTS=$(gcloud iam service-accounts list --project="$PROJECT_ID" \
  --filter="email=${SCALER_SERVICE_ACCOUNT}" --format='value(email)')
if [ -z "$ACCOUNTS" ]; then
  gcloud iam service-accounts create vma-pool-scheduler --project="$PROJECT_ID" --quiet
fi
# API, worker and scaler share this runtime identity.
ROLES=$(gcloud iam roles list --project="$PROJECT_ID" \
  --filter="name=projects/${PROJECT_ID}/roles/vmaWorkerPoolScaler" --format='value(name)')
ROLE_ACTION=create
if [ -n "$ROLES" ]; then ROLE_ACTION=update; fi
gcloud iam roles "$ROLE_ACTION" vmaWorkerPoolScaler --project="$PROJECT_ID" \
  --title='VMA worker pool scaler' \
  --permissions=run.workerpools.get,run.workerpools.update --stage=GA --quiet
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="projects/${PROJECT_ID}/roles/vmaWorkerPoolScaler" --quiet
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role=roles/iam.serviceAccountUser --quiet

# Minute ticks must NOT keep the larger, instance-billed API permanently warm.
# This private service uses request billing and one bounded DB connection.
SCALER_NAME="${SERVICE_NAME}-pool-scaler"
SCALER_URL=$(gcloud run services list --project="$PROJECT_ID" --region="$REGION" \
  --filter="metadata.name=${SCALER_NAME}" --format='value(status.url)')
sed -e "s|IMAGE_URL|${IMAGE}|g" \
  -e "s|__SERVICE_NAME__|${SERVICE_NAME}|g" \
  -e "s|__PROJECT_ID__|${PROJECT_ID}|g" \
  -e "s|__APP_ENV__|${1}|g" \
  -e "s|__REGION__|${REGION}|g" \
  -e "s|__SECRET_SUFFIX__|${SUFFIX}|g" \
  -e "s|__SCALER_URL__|${SCALER_URL}|g" \
  "${REPO_ROOT}/worker-pool-scaler.yaml" | \
  gcloud run services replace /dev/stdin --project="$PROJECT_ID" --region="$REGION" --quiet
if [ -z "$SCALER_URL" ]; then
  SCALER_URL=$(gcloud run services describe "$SCALER_NAME" --project="$PROJECT_ID" \
    --region="$REGION" --format='value(status.url)')
  case "$SCALER_URL" in https://*) ;; *) echo "Scaler URL missing." >&2; exit 1 ;; esac
  gcloud run services update "$SCALER_NAME" --project="$PROJECT_ID" --region="$REGION" \
    --update-env-vars="VMA_SCALER_AUDIENCE=${SCALER_URL}" --quiet
fi
gcloud run services add-iam-policy-binding "$SCALER_NAME" --project="$PROJECT_ID" \
  --region="$REGION" --member="serviceAccount:${SCALER_SERVICE_ACCOUNT}" \
  --role=roles/run.invoker --quiet

JOB="${SERVICE_NAME}-pool-reconcile"
JOBS=$(gcloud scheduler jobs list --project="$PROJECT_ID" --location="$REGION" \
  --filter="name=projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}" --format='value(name)')
JOB_ACTION=create
if [ -n "$JOBS" ]; then JOB_ACTION=update; fi
gcloud scheduler jobs "$JOB_ACTION" http "$JOB" --project="$PROJECT_ID" \
  --location="$REGION" --schedule='* * * * *' --time-zone=Etc/UTC \
  --uri="${SCALER_URL}/internal/worker-pool/reconcile" --http-method=POST \
  --oidc-service-account-email="$SCALER_SERVICE_ACCOUNT" --oidc-token-audience="$SCALER_URL" \
  --attempt-deadline=30s --max-retry-attempts=1 --min-backoff=10s --max-backoff=30s --quiet
