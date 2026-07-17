#!/bin/sh
# Provision an operator-owned API key in Secret Manager, then idempotently
# bootstrap its digest into the target VMA database. The plaintext is never
# written to stdout, a file, a command-line argument, or a Cloud Run setting.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

TARGET=${1:?Usage: $0 <staging|production>}
case "$TARGET" in
  staging)
    SECRET_SUFFIX="-staging"
    DEFAULT_ORGANIZATION_ID="org_votrix_staging"
    DEFAULT_ORGANIZATION_SLUG="votrix-staging"
    DEFAULT_ORGANIZATION_NAME="Votrix Staging"
    ;;
  production)
    SECRET_SUFFIX=""
    DEFAULT_ORGANIZATION_ID="org_votrix"
    DEFAULT_ORGANIZATION_SLUG="votrix"
    DEFAULT_ORGANIZATION_NAME="Votrix"
    ;;
  *)
    echo "Usage: $0 <staging|production>" >&2
    exit 2
    ;;
esac

DATABASE_SECRET="vma-database-url${SECRET_SUFFIX}"
OPERATOR_SECRET="vma-operator-api-key${SECRET_SUFFIX}"
ORGANIZATION_ID=${VMA_BOOTSTRAP_ORGANIZATION_ID:-$DEFAULT_ORGANIZATION_ID}
ORGANIZATION_SLUG=${VMA_BOOTSTRAP_ORGANIZATION_SLUG:-$DEFAULT_ORGANIZATION_SLUG}
ORGANIZATION_NAME=${VMA_BOOTSTRAP_ORGANIZATION_NAME:-$DEFAULT_ORGANIZATION_NAME}

create_operator_version() {
  APP_ENV="$TARGET" uv run --project "$REPO_ROOT" python -c \
    'from app.db.queries.api_keys import generate_api_key; print(generate_api_key())' | \
    gcloud secrets versions add "$OPERATOR_SECRET" \
      --project="$PROJECT_ID" \
      --data-file=- \
      --quiet >/dev/null
}

if gcloud secrets describe "$OPERATOR_SECRET" \
  --project="$PROJECT_ID" >/dev/null 2>&1; then
  ENABLED_VERSION=$(gcloud secrets versions list "$OPERATOR_SECRET" \
    --project="$PROJECT_ID" \
    --filter='state=ENABLED' \
    --limit=1 \
    --format='value(name)')
  if [ -z "$ENABLED_VERSION" ]; then
    echo "Adding the first enabled version to ${OPERATOR_SECRET}..."
    create_operator_version
  else
    echo "Using the existing operator secret: ${OPERATOR_SECRET}"
  fi
else
  echo "Creating operator secret: ${OPERATOR_SECRET}"
  gcloud secrets create "$OPERATOR_SECRET" \
    --project="$PROJECT_ID" \
    --replication-policy=automatic \
    --quiet >/dev/null
  create_operator_version
fi

DATABASE_URL=$(gcloud secrets versions access latest \
  --secret="$DATABASE_SECRET" \
  --project="$PROJECT_ID")
export DATABASE_URL

gcloud secrets versions access latest \
  --secret="$OPERATOR_SECRET" \
  --project="$PROJECT_ID" | \
  APP_ENV="$TARGET" uv run --project "$REPO_ROOT" python -m scripts.bootstrap_api_key \
    --organization-id "$ORGANIZATION_ID" \
    --organization-slug "$ORGANIZATION_SLUG" \
    --organization-name "$ORGANIZATION_NAME" \
    --key-name "GCP operator bootstrap" \
    --api-key-stdin \
    --redact-secret

unset DATABASE_URL
echo "Operator key is stored only in Secret Manager: ${OPERATOR_SECRET}"
