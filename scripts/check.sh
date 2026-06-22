#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
pyright

pytest -vv --timeout=60 --timeout-method=thread --ignore=tests/tools/test_execution_backend.py
pytest -vv --timeout=60 --timeout-method=thread tests/tools/test_execution_backend.py
