#!/bin/sh
# Create the Artifact Registry, dedicated runtime identity, and deploy IAM.
# Run once per project.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

REGION="${1:-$REGION}"

echo "Enabling required Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  --project="$PROJECT_ID"

if gcloud artifacts repositories describe "$REPOSITORY" \
  --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  echo "Artifact Registry already exists: ${REPOSITORY}"
else
  echo "Creating Artifact Registry: ${REPOSITORY}"
  gcloud artifacts repositories create "$REPOSITORY" \
    --project="$PROJECT_ID" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Votrix Docker images"
fi

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
echo "Registry: ${REGISTRY}/${PROJECT_ID}/${REPOSITORY}"
echo "Runtime identity: ${RUNTIME_SERVICE_ACCOUNT}"
echo "Done. Secret access is granted per secret by 1-create-secrets.sh."
