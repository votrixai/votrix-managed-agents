#!/bin/sh

# Shared Google Cloud deployment configuration.
PROJECT_ID="votrixai-480422"
REGION="us-central1"
REGISTRY="us-central1-docker.pkg.dev"
REPOSITORY="votrix"

PRODUCTION_SERVICE="votrix-managed-agents"
STAGING_SERVICE="votrix-managed-agents-staging"
PRODUCTION_WORKER_SERVICE="${PRODUCTION_SERVICE}-worker"
STAGING_WORKER_SERVICE="${STAGING_SERVICE}-worker"

PRODUCTION_TASKS_QUEUE="vma-turns"
STAGING_TASKS_QUEUE="vma-turns-staging"
TASKS_LOCATION="$REGION"

RUNTIME_SERVICE_ACCOUNT_NAME="vma-runtime"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
