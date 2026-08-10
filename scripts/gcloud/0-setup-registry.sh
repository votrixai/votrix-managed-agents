#!/bin/sh
# Create the regional Artifact Registries, dedicated runtime identity, and
# deploy IAM. Run once per project and again when adding a runtime region.

set -e

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

echo "Enabling required Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  cloudtasks.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  --project="$PROJECT_ID"

ensure_registry() {
  LOCATION=$1
  if gcloud artifacts repositories describe "$REPOSITORY" \
    --project="$PROJECT_ID" --location="$LOCATION" >/dev/null 2>&1; then
    echo "Artifact Registry already exists: ${LOCATION}/${REPOSITORY}"
  else
    echo "Creating Artifact Registry: ${LOCATION}/${REPOSITORY}"
    gcloud artifacts repositories create "$REPOSITORY" \
      --project="$PROJECT_ID" \
      --repository-format=docker \
      --location="$LOCATION" \
      --description="Votrix Docker images"
  fi
}

case "$TARGET" in
  production)
    ensure_registry "$PRODUCTION_REGION"
    ;;
  staging)
    ensure_registry "$STAGING_REGION"
    ;;
  all)
    ensure_registry "$PRODUCTION_REGION"
    if [ "$STAGING_REGION" != "$PRODUCTION_REGION" ]; then
      ensure_registry "$STAGING_REGION"
    fi
    ;;
esac

if gcloud iam service-accounts describe "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "Runtime service account already exists: ${RUNTIME_SERVICE_ACCOUNT}"
else
  echo "Creating dedicated runtime service account: ${RUNTIME_SERVICE_ACCOUNT}"
  gcloud iam service-accounts create "$RUNTIME_SERVICE_ACCOUNT_NAME" \
    --project="$PROJECT_ID" \
    --display-name="Votrix Managed Agents runtime"
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
BUILD_SERVICE_ACCOUNT=$(gcloud builds get-default-service-account \
  --project="$PROJECT_ID" 2>/dev/null || true)
if [ -z "$BUILD_SERVICE_ACCOUNT" ]; then
  BUILD_SERVICE_ACCOUNT="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
fi

echo "Granting Cloud Build the deploy permissions it needs..."
for role in \
  roles/cloudbuild.builds.builder \
  roles/artifactregistry.writer \
  roles/run.admin
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
    --role="$role" \
    --quiet
done

echo "Allowing Cloud Build to deploy as the dedicated runtime identity..."
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet

echo ""
case "$TARGET" in
  production|all)
    echo "Production registry: ${PRODUCTION_REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}"
    ;;
esac
case "$TARGET" in
  staging|all)
    echo "Staging registry: ${STAGING_REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}"
    ;;
esac
echo "Runtime identity: ${RUNTIME_SERVICE_ACCOUNT}"
echo "Done. Secret access is granted per secret by 1-create-secrets.sh."
