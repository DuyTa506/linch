#!/usr/bin/env bash
# usage: DEEPSEEK_API_KEY=... [E2E_DB=...] run.sh [--mock] scenario...
# Live mode needs DEEPSEEK_API_KEY in the caller's environment (never commit or print it); --mock needs none.
# Run the balance guard first and stop below 0.50 USD. E2E_RESULTS is where the JSON results go.
set -u
here=$(dirname "$(readlink -f "$0")")
if [[ " $* " != *" --mock "* && -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set (use --mock for the rehearsal)" >&2
  exit 2
fi
exec python "$here/ds_e2e.py" "$@"
