#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBSITE_DIR="${PROJECT_ROOT}/website"

cd "$PROJECT_ROOT"

PORT="${PORT:-8080}"
DOCS_HOST="${DOCS_HOST:-127.0.0.1}"
DOCS_PORT="${DOCS_PORT:-4180}"
RUN_MIGRATIONS=false
DOCS_PID=""

cleanup_docs() {
  local exit_code=$?

  if [[ -n "$DOCS_PID" ]] && kill -0 "$DOCS_PID" 2>/dev/null; then
    echo
    echo "Stopping local documentation server..."
    kill "$DOCS_PID" 2>/dev/null || true
    wait "$DOCS_PID" 2>/dev/null || true
  fi

  return "$exit_code"
}

trap cleanup_docs EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --migrate|-m)
      RUN_MIGRATIONS=true
      shift
      ;;
    --help|-h)
      echo "Usage: bash run.sh [--migrate|-m]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: bash run.sh [--migrate|-m]" >&2
      exit 1
      ;;
  esac
done

uv sync --extra sandbox-e2b --extra dev
source .venv/bin/activate

if [[ "$RUN_MIGRATIONS" == true ]]; then
  alembic upgrade head
else
  echo "Skipping Alembic migrations. Run with --migrate or -m to apply them."
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to start the local documentation site (Node.js 22+)." >&2
  exit 1
fi

NEXT_BIN="${WEBSITE_DIR}/node_modules/.bin/next"
NPM_INSTALL_STAMP="${WEBSITE_DIR}/node_modules/.package-lock.json"

if [[ ! -x "$NEXT_BIN" ]] || [[ ! -f "$NPM_INSTALL_STAMP" ]] || \
  [[ "${WEBSITE_DIR}/package-lock.json" -nt "$NPM_INSTALL_STAMP" ]]; then
  echo "Installing local documentation dependencies..."
  npm --prefix "$WEBSITE_DIR" install
fi

if [[ ! "$DOCS_PORT" =~ ^[0-9]+$ ]] || (( DOCS_PORT < 1 || DOCS_PORT > 65535 )); then
  echo "DOCS_PORT must be an integer between 1 and 65535." >&2
  exit 1
fi

docs_port_is_free() {
  python - "$1" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    try:
        server.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1)
PY
}

REQUESTED_DOCS_PORT="$DOCS_PORT"
while ! docs_port_is_free "$DOCS_PORT"; do
  DOCS_PORT=$((DOCS_PORT + 1))
  if (( DOCS_PORT > 65535 || DOCS_PORT > REQUESTED_DOCS_PORT + 50 )); then
    echo "Could not find a free documentation port near ${REQUESTED_DOCS_PORT}." >&2
    exit 1
  fi
done

if [[ "$DOCS_PORT" != "$REQUESTED_DOCS_PORT" ]]; then
  echo "Documentation port ${REQUESTED_DOCS_PORT} is in use; using ${DOCS_PORT} instead."
fi

echo "Starting local documentation server on port ${DOCS_PORT}..."
(
  cd "$WEBSITE_DIR"
  exec "$NEXT_BIN" dev --webpack --hostname "$DOCS_HOST" --port "$DOCS_PORT"
) &
DOCS_PID=$!

DOCS_BASE_URL="http://localhost:${DOCS_PORT}"
DOCS_READY=false
for ((attempt = 1; attempt <= 150; attempt++)); do
  if curl --silent --fail --output /dev/null "${DOCS_BASE_URL}/"; then
    DOCS_READY=true
    break
  fi

  if ! kill -0 "$DOCS_PID" 2>/dev/null; then
    break
  fi

  sleep 0.2
done

if [[ "$DOCS_READY" != true ]]; then
  echo "The local documentation server did not become ready." >&2
  if ! kill -0 "$DOCS_PID" 2>/dev/null; then
    wait "$DOCS_PID" || true
  fi
  exit 1
fi

echo
echo "Local documentation:"
echo "  Home:           ${DOCS_BASE_URL}/"
echo "  Documentation:  ${DOCS_BASE_URL}/docs/"
echo "  API Playground: ${DOCS_BASE_URL}/docs/api/agents/list_agents_v1_agents_get/"
echo
echo "OpenAPI schema: http://127.0.0.1:${PORT}/openapi.json"
uvicorn votrix_managed_agents:create_app --factory --host 0.0.0.0 --port "$PORT" --reload
