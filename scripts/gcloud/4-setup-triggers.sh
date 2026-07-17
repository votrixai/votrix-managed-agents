#!/bin/sh
# Create or update Cloud Build triggers for production and staging auto-deploys.
# Connect the GitHub repository in Cloud Build before running this script.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

OWNER="${1:?Usage: $0 <github-owner> <repo-name>}"
REPO="${2:?Usage: $0 <github-owner> <repo-name>}"
TRIGGER_REGION="${VMA_TRIGGER_REGION:-global}"
PRODUCTION_REQUIRE_APPROVAL="${VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL:-true}"
IGNORED_FILES="docs/**,website/**,sdks/**,README.md,CHANGELOG.md"

case "$PRODUCTION_REQUIRE_APPROVAL" in
  true)
    PRODUCTION_APPROVAL_FLAG="--require-approval"
    ;;
  false)
    PRODUCTION_APPROVAL_FLAG="--no-require-approval"
    ;;
  *)
    echo "VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL must be true or false." >&2
    exit 2
    ;;
esac

upsert_trigger() {
  TRIGGER_NAME=$1
  BRANCH_PATTERN=$2
  SERVICE_NAME=$3
  SERVICE_CONFIG=$4
  APP_ENV=$5
  SECRET_SUFFIX=$6
  APPROVAL_FLAG=$7
  SUBSTITUTIONS="_SERVICE_NAME=${SERVICE_NAME},_SERVICE_CONFIG=${SERVICE_CONFIG},_REPO=${REPOSITORY},_REGION=${REGION},_APP_ENV=${APP_ENV},_SECRET_SUFFIX=${SECRET_SUFFIX}"

  if gcloud builds triggers describe "$TRIGGER_NAME" \
    --project="$PROJECT_ID" \
    --region="$TRIGGER_REGION" \
    --quiet >/dev/null 2>&1; then
    echo "Updating trigger: ${TRIGGER_NAME}"
    gcloud builds triggers update github "$TRIGGER_NAME" \
      --project="$PROJECT_ID" \
      --region="$TRIGGER_REGION" \
      --repo-name="$REPO" \
      --repo-owner="$OWNER" \
      --branch-pattern="$BRANCH_PATTERN" \
      --build-config=cloudbuild.yaml \
      --ignored-files="$IGNORED_FILES" \
      --update-substitutions="$SUBSTITUTIONS" \
      "$APPROVAL_FLAG" \
      --quiet
  else
    echo "Creating trigger: ${TRIGGER_NAME}"
    gcloud builds triggers create github \
      --project="$PROJECT_ID" \
      --region="$TRIGGER_REGION" \
      --repo-name="$REPO" \
      --repo-owner="$OWNER" \
      --branch-pattern="$BRANCH_PATTERN" \
      --build-config=cloudbuild.yaml \
      --ignored-files="$IGNORED_FILES" \
      --name="$TRIGGER_NAME" \
      --substitutions="$SUBSTITUTIONS" \
      "$APPROVAL_FLAG" \
      --quiet
  fi
}

upsert_trigger \
  vma-deploy-production \
  "^main$" \
  "$PRODUCTION_SERVICE" \
  service.production.yaml \
  production \
  "" \
  "$PRODUCTION_APPROVAL_FLAG"

upsert_trigger \
  vma-deploy-staging \
  "^staging$" \
  "$STAGING_SERVICE" \
  service.staging.yaml \
  staging \
  "-staging" \
  --no-require-approval

if [ "$PRODUCTION_REQUIRE_APPROVAL" = "true" ]; then
  echo "Done. main queues an approval-gated production build; staging deploys staging automatically."
else
  echo "Done. main and staging deploy their environments automatically."
fi
