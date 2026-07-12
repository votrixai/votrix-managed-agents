#!/bin/sh

# Shared Google Cloud deployment configuration.
PROJECT_ID="votrixai-480422"
REGION="us-central1"
REGISTRY="us-central1-docker.pkg.dev"
REPOSITORY="votrix"

PRODUCTION_SERVICE="votrix-managed-agents"
STAGING_SERVICE="votrix-managed-agents-staging"

RUNTIME_SERVICE_ACCOUNT_NAME="vma-runtime"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
