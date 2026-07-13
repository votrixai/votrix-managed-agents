#!/bin/bash
set -e

PORT="${PORT:-8080}"
RUN_MIGRATIONS=false

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

uv sync --extra sandbox-e2b
source .venv/bin/activate

if [[ "$RUN_MIGRATIONS" == true ]]; then
  alembic upgrade head
else
  echo "Skipping Alembic migrations. Run with --migrate or -m to apply them."
fi

echo "Scalar API docs: http://127.0.0.1:${PORT}/docs"
uvicorn votrix_managed_agents:create_app --factory --host 0.0.0.0 --port "$PORT" --reload
