#!/bin/sh
set -e

alembic upgrade "${ALEMBIC_TARGET:-head}"
python -m app.runtime.checkpoint_setup
