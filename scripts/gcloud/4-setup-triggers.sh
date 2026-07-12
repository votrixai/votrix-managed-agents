#!/bin/sh
# Create Cloud Build triggers for production and staging auto-deploys.
# Connect the GitHub repository in Cloud Build before running this script.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

OWNER="${1:?Usage: $0 <github-owner> <repo-name>}"
REPO="${2:?Usage: $0 <github-owner> <repo-name>}"

echo "Creating production trigger: vma-deploy-production"
gcloud builds triggers create github \
  --project="$PROJECT_ID" \
  --repo-name="$REPO" \
  --repo-owner="$OWNER" \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml \
  --name=vma-deploy-production \
  --substitutions=_SERVICE_NAME="${PRODUCTION_SERVICE}",_SERVICE_CONFIG="service.production.yaml",_REPO="${REPOSITORY}",_REGION="${REGION}",_APP_ENV="production"

echo "Creating staging trigger: vma-deploy-staging"
gcloud builds triggers create github \
  --project="$PROJECT_ID" \
  --repo-name="$REPO" \
  --repo-owner="$OWNER" \
  --branch-pattern="^beta$" \
  --build-config=cloudbuild.yaml \
  --name=vma-deploy-staging \
  --substitutions=_SERVICE_NAME="${STAGING_SERVICE}",_SERVICE_CONFIG="service.staging.yaml",_REPO="${REPOSITORY}",_REGION="${REGION}",_APP_ENV="staging",_SECRET_SUFFIX="-staging"

echo "Done. main deploys production; beta deploys staging."
