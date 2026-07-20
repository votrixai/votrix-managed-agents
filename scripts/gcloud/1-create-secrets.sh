#!/bin/sh
# Create or update the exact Secret Manager values consumed by VMA.
#
# Usage:
#   ./scripts/gcloud/1-create-secrets.sh /path/to/production.env
#   ./scripts/gcloud/1-create-secrets.sh /path/to/staging.env --suffix staging

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

ENV_FILE="${1:?Usage: $0 <env-file> [--suffix staging]}"
SECRET_SUFFIX=""
ENVIRONMENT="production"

shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --suffix)
      [ "$#" -ge 2 ] || { echo "--suffix requires a value" >&2; exit 1; }
      if [ "$2" != "staging" ]; then
        echo "Only --suffix staging is supported." >&2
        exit 1
      fi
      SECRET_SUFFIX="-staging"
      ENVIRONMENT="staging"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "File not found: $ENV_FILE" >&2
  exit 1
fi

read_required_value() {
  variable_name="$1"
  value=$(sed -n "s/^${variable_name}=//p" "$ENV_FILE" | tail -n 1)
  if [ -z "$value" ]; then
    echo "Missing or empty required value: ${variable_name}" >&2
    exit 1
  fi
  case "$value" in
    *PROJECT_REF*|*URL_ENCODED_PASSWORD*|*ACCOUNT_ID*|replace-with-*)
      echo "Placeholder has not been replaced: ${variable_name}" >&2
      exit 1
      ;;
  esac
  case "$variable_name:$value" in
    DATABASE_URL:postgresql+asyncpg://*:6543/*) ;;
    DATABASE_URL:*)
      echo "DATABASE_URL must use the postgresql+asyncpg transaction-pooler URL on port 6543." >&2
      exit 1
      ;;
    VMA_LISTEN_DATABASE_URL:postgresql+asyncpg://*:5432/*) ;;
    VMA_LISTEN_DATABASE_URL:*)
      echo "VMA_LISTEN_DATABASE_URL must use the postgresql+asyncpg session-mode URL on port 5432." >&2
      exit 1
      ;;
    DATABASE_URL_DIRECT:postgresql+asyncpg://*:5432/*) ;;
    DATABASE_URL_DIRECT:*)
      echo "DATABASE_URL_DIRECT must use a postgresql+asyncpg session/direct URL on port 5432." >&2
      exit 1
      ;;
  esac
  printf '%s' "$value"
}

echo "Importing the approved ${ENVIRONMENT} VMA secrets..."
while IFS='|' read -r variable_name base_secret_name; do
  [ -n "$variable_name" ] || continue
  secret_name="${base_secret_name}${SECRET_SUFFIX}"
  secret_value=$(read_required_value "$variable_name")

  if gcloud secrets describe "$secret_name" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Updating secret: ${secret_name}"
    printf '%s' "$secret_value" | gcloud secrets versions add "$secret_name" \
      --project="$PROJECT_ID" \
      --data-file=- \
      --quiet
  else
    echo "Creating secret: ${secret_name}"
    printf '%s' "$secret_value" | gcloud secrets create "$secret_name" \
      --project="$PROJECT_ID" \
      --replication-policy=automatic \
      --data-file=- \
      --quiet
  fi

  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
done <<'SECRETS'
DATABASE_URL|vma-database-url
VMA_LISTEN_DATABASE_URL|vma-listen-database-url
DATABASE_URL_DIRECT|vma-database-url-direct
VMA_SUPABASE_URL|vma-supabase-url
VMA_SUPABASE_PUBLISHABLE_KEY|vma-supabase-publishable-key
VMA_RESEND_API_KEY|vma-resend-api-key
VMA_ENCRYPTION_KEY|vma-encryption-key
E2B_API_KEY|vma-e2b-api-key
S3_ENDPOINT_URL|vma-s3-endpoint-url
S3_ACCESS_KEY_ID|vma-s3-access-key-id
S3_SECRET_ACCESS_KEY|vma-s3-secret-access-key
S3_BUCKET_NAME|vma-s3-bucket-name
SECRETS

echo "Done. Only the allowlisted VMA secrets were imported."
