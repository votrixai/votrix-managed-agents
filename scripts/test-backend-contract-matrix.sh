#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONTRACT_TEST="tests/contract/test_votrix_backend_consumer_contract.py"

uv run --isolated --extra dev --extra contract-backend pytest "$CONTRACT_TEST"
uv run --isolated --extra dev --extra contract pytest "$CONTRACT_TEST"
