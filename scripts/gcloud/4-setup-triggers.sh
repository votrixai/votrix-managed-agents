#!/bin/sh
# Create or update regional Cloud Build triggers for production and staging.
# The GitHub repository must already be linked through a 2nd-gen connection.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

OWNER="${1:?Usage: $0 <github-owner> <repo-name>}"
REPO="${2:?Usage: $0 <github-owner> <repo-name>}"
TRIGGER_REGION="${VMA_TRIGGER_REGION:-$REGION}"
CONNECTION_NAME="${VMA_CLOUD_BUILD_CONNECTION:-votrix-github}"
LINKED_REPOSITORY="${VMA_CLOUD_BUILD_REPOSITORY:-$REPO}"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
TRIGGER_SERVICE_ACCOUNT="${VMA_CLOUD_BUILD_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
TRIGGER_SERVICE_ACCOUNT_RESOURCE="projects/${PROJECT_ID}/serviceAccounts/${TRIGGER_SERVICE_ACCOUNT}"
PRODUCTION_REQUIRE_APPROVAL="${VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL:-false}"
TRIGGER_TEMPLATE="${SCRIPT_DIR}/trigger.yaml.in"

CONNECTION_STAGE=$(gcloud builds connections describe "$CONNECTION_NAME" \
  --project="$PROJECT_ID" \
  --region="$TRIGGER_REGION" \
  --format='value(installationState.stage)' 2>/dev/null || true)
if [ "$CONNECTION_STAGE" != "COMPLETE" ]; then
  echo "Cloud Build connection is not ready: ${TRIGGER_REGION}/${CONNECTION_NAME}" >&2
  echo "Create or authorize the 2nd-gen GitHub connection before configuring triggers." >&2
  exit 1
fi

SOURCE_REPOSITORY=$(gcloud builds repositories describe "$LINKED_REPOSITORY" \
  --project="$PROJECT_ID" \
  --region="$TRIGGER_REGION" \
  --connection="$CONNECTION_NAME" \
  --format='value(name)' 2>/dev/null || true)
REMOTE_URI=$(gcloud builds repositories describe "$LINKED_REPOSITORY" \
  --project="$PROJECT_ID" \
  --region="$TRIGGER_REGION" \
  --connection="$CONNECTION_NAME" \
  --format='value(remoteUri)' 2>/dev/null || true)

case "$REMOTE_URI" in
  "https://github.com/${OWNER}/${REPO}"|"https://github.com/${OWNER}/${REPO}.git")
    ;;
  *)
    echo "Cloud Build repository is missing or points at an unexpected remote: ${REMOTE_URI:-missing}" >&2
    exit 1
    ;;
esac

case "$PRODUCTION_REQUIRE_APPROVAL" in
  true)
    PRODUCTION_APPROVAL_REQUIRED=true
    ;;
  false)
    PRODUCTION_APPROVAL_REQUIRED=false
    ;;
  *)
    echo "VMA_PRODUCTION_TRIGGER_REQUIRE_APPROVAL must be true or false." >&2
    exit 2
    ;;
esac

TEMP_TRIGGER=$(mktemp "${TMPDIR:-/tmp}/vma-cloud-build-trigger.XXXXXX")
trap 'rm -f "$TEMP_TRIGGER"' EXIT HUP INT TERM

import_trigger() {
  TRIGGER_NAME=$1
  BRANCH_PATTERN=$2
  SERVICE_NAME=$3
  SERVICE_CONFIG=$4
  APP_ENV=$5
  SECRET_SUFFIX=$6
  APPROVAL_REQUIRED=$7

  if gcloud builds triggers describe "$TRIGGER_NAME" \
    --project="$PROJECT_ID" \
    --region="$TRIGGER_REGION" \
    --quiet >/dev/null 2>&1; then
    echo "Reconciling trigger: ${TRIGGER_NAME}"
  else
    echo "Creating trigger: ${TRIGGER_NAME}"
  fi

  sed \
    -e "s|__TRIGGER_NAME__|${TRIGGER_NAME}|g" \
    -e "s|__BRANCH_PATTERN__|${BRANCH_PATTERN}|g" \
    -e "s|__SERVICE_NAME__|${SERVICE_NAME}|g" \
    -e "s|__SERVICE_CONFIG__|${SERVICE_CONFIG}|g" \
    -e "s|__APP_ENV__|${APP_ENV}|g" \
    -e "s|__SECRET_SUFFIX__|${SECRET_SUFFIX}|g" \
    -e "s|__APPROVAL_REQUIRED__|${APPROVAL_REQUIRED}|g" \
    -e "s|__SOURCE_REPOSITORY__|${SOURCE_REPOSITORY}|g" \
    -e "s|__SERVICE_ACCOUNT__|${TRIGGER_SERVICE_ACCOUNT_RESOURCE}|g" \
    -e "s|__REGION__|${REGION}|g" \
    -e "s|__ARTIFACT_REPOSITORY__|${REPOSITORY}|g" \
    "$TRIGGER_TEMPLATE" >"$TEMP_TRIGGER"

  gcloud builds triggers import \
    --source="$TEMP_TRIGGER" \
    --project="$PROJECT_ID" \
    --region="$TRIGGER_REGION" \
    --format='value(name)' \
    --quiet
}

import_trigger \
  vma-deploy-production \
  "^main$" \
  "$PRODUCTION_SERVICE" \
  service.production.yaml \
  production \
  "" \
  "$PRODUCTION_APPROVAL_REQUIRED"

import_trigger \
  vma-deploy-staging \
  "^staging$" \
  "$STAGING_SERVICE" \
  service.staging.yaml \
  staging \
  "-staging" \
  false

if [ "$PRODUCTION_REQUIRE_APPROVAL" = "true" ]; then
  echo "Done. main queues an approval-gated production build; staging deploys staging automatically."
else
  echo "Done. main and staging deploy their environments automatically."
fi
