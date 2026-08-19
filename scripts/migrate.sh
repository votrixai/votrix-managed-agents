#!/bin/sh
set -e

python scripts/rename_legacy_schema.py
alembic upgrade "${ALEMBIC_TARGET:-head}"
