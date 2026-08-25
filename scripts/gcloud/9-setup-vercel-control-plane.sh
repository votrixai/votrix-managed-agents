#!/usr/bin/env bash
# Give the VMA Developer App a keyless, least-privilege Cloud Run identity.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "${SCRIPT_DIR}/config.sh"

POOL_ID="${VMA_VERCEL_WORKLOAD_IDENTITY_POOL_ID:-vercel-vma}"
PROVIDER_ID="${VMA_VERCEL_WORKLOAD_IDENTITY_PROVIDER_ID:-vma-developer-app}"
VERCEL_TEAM_SLUG="${VMA_VERCEL_TEAM_SLUG:-cosmobiosis-projects}"
VERCEL_TEAM_ID="${VMA_VERCEL_TEAM_ID:-team_5ibBLYeElwspYJYukJKUD1w8}"
VERCEL_PROJECT_NAME="${VMA_VERCEL_PROJECT_NAME:-vma-developer-app}"
VERCEL_PROJECT_ID="${VMA_VERCEL_PROJECT_ID:-prj_N3Oxqn5GJudFcH16ctPkkxGQkEmw}"
ISSUER_URI="https://oidc.vercel.com/${VERCEL_TEAM_SLUG}"
ATTRIBUTE_MAPPING="google.subject=assertion.sub,attribute.owner_id=assertion.owner_id,attribute.project_id=assertion.project_id"
ATTRIBUTE_CONDITION="assertion.owner_id == '${VERCEL_TEAM_ID}' && assertion.project_id == '${VERCEL_PROJECT_ID}'"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')
if [ -z "$PROJECT_NUMBER" ]; then
  echo "Could not resolve the Google Cloud project number." >&2
  exit 1
fi

echo "Enabling the keyless identity APIs..."
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project="$PROJECT_ID" \
  --quiet

if ! gcloud iam workload-identity-pools describe "$POOL_ID" \
  --project="$PROJECT_ID" \
  --location=global \
  --quiet >/dev/null 2>&1; then
  echo "Creating Workload Identity Pool ${POOL_ID}..."
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$PROJECT_ID" \
    --location=global \
    --display-name="Vercel VMA" \
    --description="Keyless identity for the VMA Developer App" \
    --quiet
fi

if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --project="$PROJECT_ID" \
  --location=global \
  --workload-identity-pool="$POOL_ID" \
  --quiet >/dev/null 2>&1; then
  echo "Reconciling the Vercel OIDC provider..."
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" \
    --location=global \
    --workload-identity-pool="$POOL_ID" \
    --display-name="VMA Developer App" \
    --issuer-uri="$ISSUER_URI" \
    --attribute-mapping="$ATTRIBUTE_MAPPING" \
    --attribute-condition="$ATTRIBUTE_CONDITION" \
    --quiet
else
  echo "Creating the Vercel OIDC provider..."
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" \
    --location=global \
    --workload-identity-pool="$POOL_ID" \
    --display-name="VMA Developer App" \
    --issuer-uri="$ISSUER_URI" \
    --attribute-mapping="$ATTRIBUTE_MAPPING" \
    --attribute-condition="$ATTRIBUTE_CONDITION" \
    --quiet
fi

if ! gcloud iam service-accounts describe "$VERCEL_INVOKER_SERVICE_ACCOUNT" \
  --project="$PROJECT_ID" \
  --quiet >/dev/null 2>&1; then
  echo "Creating ${VERCEL_INVOKER_SERVICE_ACCOUNT}..."
  gcloud iam service-accounts create "$VERCEL_INVOKER_SERVICE_ACCOUNT_NAME" \
    --project="$PROJECT_ID" \
    --display-name="VMA Developer App Cloud Run invoker" \
    --description="Mints only short-lived ID tokens for the private VMA control plane" \
    --quiet
fi

for environment in production staging; do
  subject="owner:${VERCEL_TEAM_SLUG}:project:${VERCEL_PROJECT_NAME}:environment:${environment}"
  principal="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/subject/${subject}"
  echo "Allowing the exact Vercel ${environment} deployment identity to mint ID tokens..."
  gcloud iam service-accounts add-iam-policy-binding \
    "$VERCEL_INVOKER_SERVICE_ACCOUNT" \
    --project="$PROJECT_ID" \
    --member="$principal" \
    --role="roles/iam.workloadIdentityUser" \
    --quiet >/dev/null
done

for service_and_region in \
  "${PRODUCTION_CONTROL_PLANE_SERVICE}:${PRODUCTION_REGION}" \
  "${STAGING_CONTROL_PLANE_SERVICE}:${STAGING_REGION}"; do
  service=${service_and_region%%:*}
  region=${service_and_region#*:}
  if gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$region" \
    --quiet >/dev/null 2>&1; then
    echo "Granting ${VERCEL_INVOKER_SERVICE_ACCOUNT} Invoker on ${service}..."
    gcloud run services add-iam-policy-binding "$service" \
      --project="$PROJECT_ID" \
      --region="$region" \
      --member="serviceAccount:${VERCEL_INVOKER_SERVICE_ACCOUNT}" \
      --role="roles/run.invoker" \
      --quiet >/dev/null
  fi
done

AUDIENCE="https://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
echo
echo "Vercel non-secret configuration:"
echo "GCP_PROJECT_NUMBER=${PROJECT_NUMBER}"
echo "GCP_WORKLOAD_IDENTITY_POOL_ID=${POOL_ID}"
echo "GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID=${PROVIDER_ID}"
echo "GCP_SERVICE_ACCOUNT_EMAIL=${VERCEL_INVOKER_SERVICE_ACCOUNT}"
echo "GCP_AUDIENCE=${AUDIENCE}"
