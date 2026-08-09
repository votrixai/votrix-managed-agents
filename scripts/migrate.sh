#!/bin/sh
set -e

alembic upgrade "${ALEMBIC_TARGET:-head}"
