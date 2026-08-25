#!/bin/sh

# Shared Google Cloud deployment configuration.
PROJECT_ID="votrixai-480422"
REPOSITORY="votrix"

# Keep each runtime close to its environment's Supabase database. Production
# Supabase is in AWS us-east-1 (Northern Virginia); staging is in AWS us-west-1
# (Northern California), whose closest supported GCP candidates are on the US
# west coast. These may be overridden for an intentional one-off migration or
# latency comparison without moving the regional Cloud Build source connection.
PRODUCTION_REGION="${VMA_PRODUCTION_REGION:-us-east4}"
STAGING_REGION="${VMA_STAGING_REGION:-us-west2}"
CLOUD_BUILD_REGION="${VMA_CLOUD_BUILD_REGION:-us-central1}"

PRODUCTION_SERVICE="votrix-managed-agents"
STAGING_SERVICE="votrix-managed-agents-staging"
PRODUCTION_WORKER_SERVICE="${PRODUCTION_SERVICE}-worker"
STAGING_WORKER_SERVICE="${STAGING_SERVICE}-worker"
PRODUCTION_CONTROL_PLANE_SERVICE="${PRODUCTION_SERVICE}-control-plane"
STAGING_CONTROL_PLANE_SERVICE="${STAGING_SERVICE}-control-plane"

PRODUCTION_TASKS_QUEUE="vma-turns"
STAGING_TASKS_QUEUE="vma-turns-staging"

RUNTIME_SERVICE_ACCOUNT_NAME="vma-runtime"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
VERCEL_INVOKER_SERVICE_ACCOUNT_NAME="vma-developer-app-invoker"
VERCEL_INVOKER_SERVICE_ACCOUNT="${VERCEL_INVOKER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
