#!/usr/bin/env bash
# Provision an operator-owned API key in Secret Manager, then idempotently
# bootstrap its digest into the target VMA database. The plaintext only crosses
# a pipe between trusted commands; it is not exposed to the terminal or logs,
# persisted to a file, passed as a command-line argument, or mounted at runtime.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

TARGET=${1:?Usage: $0 <staging|production>}
case "$TARGET" in
  staging)
    SECRET_SUFFIX="-staging"
    DEFAULT_ORGANIZATION_ID="org_019fda5a22b2725cb1360e7f8ee6f0e7"
    DEFAULT_ORGANIZATION_NAME="Votrix AI"
    DEFAULT_DATABASE_SCHEMA="vma_rewrite_staging"
    ;;
  production)
    SECRET_SUFFIX=""
    DEFAULT_ORGANIZATION_ID="org_votrix"
    DEFAULT_ORGANIZATION_NAME="Votrix"
    DEFAULT_DATABASE_SCHEMA="vma_rewrite_production"
    ;;
  *)
    echo "Usage: $0 <staging|production>" >&2
    exit 2
    ;;
esac

DATABASE_SECRET="vma-database-url${SECRET_SUFFIX}"
OPERATOR_SECRET="vma-operator-api-key${SECRET_SUFFIX}"
ORGANIZATION_ID=${VMA_BOOTSTRAP_ORGANIZATION_ID:-$DEFAULT_ORGANIZATION_ID}
ORGANIZATION_NAME=${VMA_BOOTSTRAP_ORGANIZATION_NAME:-$DEFAULT_ORGANIZATION_NAME}
DATABASE_SCHEMA=${VMA_BOOTSTRAP_DATABASE_SCHEMA:-$DEFAULT_DATABASE_SCHEMA}

create_operator_version() {
  APP_ENV="$TARGET" uv run --project "$REPO_ROOT" python -c \
    'from app.db.queries.vma_api_keys import generate_vma_api_key; print(generate_vma_api_key())' | \
    gcloud secrets versions add "$OPERATOR_SECRET" \
      --project="$PROJECT_ID" \
      --data-file=- \
      --quiet \
      --format='value(name.basename())'
}

if gcloud secrets describe "$OPERATOR_SECRET" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  ENABLED_VERSION=$(gcloud secrets versions list "$OPERATOR_SECRET" \
    --project="$PROJECT_ID" \
    --filter='state=ENABLED' \
    --sort-by='~createTime' \
    --limit=1 \
    --format='value(name.basename())')
  if [ -z "$ENABLED_VERSION" ]; then
    echo "Adding the first enabled version to ${OPERATOR_SECRET}..."
    ENABLED_VERSION=$(create_operator_version)
  else
    echo "Using the existing operator secret: ${OPERATOR_SECRET}"
  fi
else
  echo "Creating operator secret: ${OPERATOR_SECRET}"
  gcloud secrets create "$OPERATOR_SECRET" \
    --project="$PROJECT_ID" \
    --replication-policy=automatic \
    --quiet >/dev/null
  ENABLED_VERSION=$(create_operator_version)
fi

DATABASE_URL=$(gcloud secrets versions access latest \
  --secret="$DATABASE_SECRET" \
  --project="$PROJECT_ID")
export DATABASE_URL
export DATABASE_SCHEMA

gcloud secrets versions access "$ENABLED_VERSION" \
  --secret="$OPERATOR_SECRET" \
  --project="$PROJECT_ID" | \
  APP_ENV="$TARGET" uv run --project "$REPO_ROOT" python -m scripts.bootstrap_api_key \
    --organization-id "$ORGANIZATION_ID" \
    --organization-name "$ORGANIZATION_NAME" \
    --key-name "GCP operator bootstrap" \
    --api-key-stdin \
    --redact-secret

unset DATABASE_URL DATABASE_SCHEMA
echo "Operator key is stored only in Secret Manager: ${OPERATOR_SECRET}"
