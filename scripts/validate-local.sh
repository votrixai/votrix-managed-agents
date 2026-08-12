#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  printf '%s\n' \
    "Usage: ./scripts/validate-local.sh [backend] [docs] [router]" \
    "" \
    "Run the checks removed from GitHub Actions on the local machine." \
    "With no targets, all three validation groups run." \
    "" \
    "  backend  Shell validation and pytest on Python 3.12 and 3.13" \
    "  docs     Documentation type-check, lint, build, and Wrangler dry-run" \
    "  router   API router checks and staging/production Wrangler dry-runs"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

heading() {
  printf '\n==> %s\n' "$1"
}

validate_backend() {
  require_command uv

  heading "Validate shell entrypoints"
  (
    cd "$ROOT_DIR"
    sh -n entrypoint.sh scripts/*.sh scripts/gcloud/*.sh
    bash -n run.sh
  )

  for python_version in 3.12 3.13; do
    heading "Run pytest on Python ${python_version}"
    (
      cd "$ROOT_DIR"
      uv run \
        --isolated \
        --frozen \
        --python "$python_version" \
        --extra dev \
        --extra sandbox-e2b \
        pytest
    )
  done
}

validate_docs() {
  require_command npm

  heading "Install documentation dependencies"
  (
    cd "$ROOT_DIR/website"
    npm ci
    npm run typecheck
    npm run lint
    npm run build
    npm exec wrangler -- deploy --dry-run --env=""
  )
}

validate_router() {
  require_command npm

  heading "Install API router dependencies"
  (
    cd "$ROOT_DIR/infra/cloudflare/vma-api-router"
    npm ci
    npm run check
    npm run dry-run:staging
    npm run dry-run:production
  )
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if (( $# == 0 )); then
  set -- backend docs router
fi

for target in "$@"; do
  case "$target" in
    backend)
      validate_backend
      ;;
    docs)
      validate_docs
      ;;
    router)
      validate_router
      ;;
    *)
      printf 'Unknown validation target: %s\n\n' "$target" >&2
      usage >&2
      exit 2
      ;;
  esac
done

heading "Local validation passed"
