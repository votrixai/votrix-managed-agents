#!/bin/sh
set -e

exec alembic upgrade "${ALEMBIC_TARGET:-head}"
