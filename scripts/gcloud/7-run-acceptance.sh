#!/bin/sh
# Provision an encrypted BYOK Vault from an operator-selected Secret Manager
# source, then run the real Postgres/R2/E2B/model acceptance smoke.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
. "${SCRIPT_DIR}/config.sh"

TARGET=${1:?Usage: $0 <staging|production>}
case "$TARGET" in
  staging)
    SECRET_SUFFIX="-staging"
    SERVICE_NAME=$STAGING_SERVICE
    ;;
  production)
    SECRET_SUFFIX=""
    SERVICE_NAME=$PRODUCTION_SERVICE
    ;;
  *)
    echo "Usage: $0 <staging|production>" >&2
    exit 2
    ;;
esac

OPERATOR_SECRET="vma-operator-api-key${SECRET_SUFFIX}"
MODEL_SECRET=${VMA_SMOKE_MODEL_API_KEY_SECRET:?
Set VMA_SMOKE_MODEL_API_KEY_SECRET to an operator-owned Secret Manager name}
BASE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(status.url)')

if [ -z "$BASE_URL" ]; then
  echo "Cloud Run service is not deployed: ${SERVICE_NAME}" >&2
  exit 1
fi

VMA_SMOKE_API_KEY=$(gcloud secrets versions access latest \
  --secret="$OPERATOR_SECRET" \
  --project="$PROJECT_ID")
VMA_SMOKE_MODEL_API_KEY=$(gcloud secrets versions access latest \
  --secret="$MODEL_SECRET" \
  --project="$PROJECT_ID")
VMA_SMOKE_BASE_URL=$BASE_URL
export VMA_SMOKE_API_KEY VMA_SMOKE_MODEL_API_KEY VMA_SMOKE_BASE_URL

PROVISIONED=$(uv run --project "$REPO_ROOT" python \
  "${REPO_ROOT}/scripts/provision_smoke_vault.py")
echo "$PROVISIONED"
VMA_SMOKE_VAULT_IDS=$(printf '%s' "$PROVISIONED" | uv run --project "$REPO_ROOT" python -c \
  'import json,sys; print(json.load(sys.stdin)["vault_id"])')
export VMA_SMOKE_VAULT_IDS
unset VMA_SMOKE_MODEL_API_KEY

uv run --project "$REPO_ROOT" --extra sandbox-e2b python \
  "${REPO_ROOT}/scripts/pilot_acceptance.py"

unset VMA_SMOKE_API_KEY VMA_SMOKE_BASE_URL VMA_SMOKE_VAULT_IDS
echo "${TARGET} acceptance passed against ${BASE_URL}"
