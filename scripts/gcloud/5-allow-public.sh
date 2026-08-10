#!/bin/sh
# Make one or both regional API services reachable publicly by disabling the
# Cloud Run Invoker IAM check. This is the same mechanism declared in the
# service manifests. Database-backed tenant API-key auth still applies at the
# application layer.

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

allow_public_api() {
  SERVICE=$1
  SERVICE_REGION=$2
  case "$SERVICE" in
    *worker*)
      echo "Refusing to make a VMA worker service public: ${SERVICE}" >&2
      exit 1
      ;;
  esac

  gcloud run services update "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$SERVICE_REGION" \
    --no-invoker-iam-check \
    --quiet
}

case "$TARGET" in
  production)
    allow_public_api "$PRODUCTION_SERVICE" "$PRODUCTION_REGION"
    ;;
  staging)
    allow_public_api "$STAGING_SERVICE" "$STAGING_REGION"
    ;;
  all)
    allow_public_api "$PRODUCTION_SERVICE" "$PRODUCTION_REGION"
    allow_public_api "$STAGING_SERVICE" "$STAGING_REGION"
    ;;
esac

echo "Selected VMA API services have the Invoker IAM check disabled."
echo "Worker services remain private; database-backed tenant API-key auth remains enabled."
