#!/usr/bin/env bash
# usage: run.sh [--mock] scenario...   (live mode sources the key here, inside the script; it is never printed)
set -u
export E2E_DB=par_2c
. /home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/env.sh
if [[ " $* " != *" --mock "* ]]; then
  set -a; . /home/aiqk3/.claude/jobs/8f7b54ed/tmp/deepseek.env; set +a
fi
exec "$E2E_PYTHON" /home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/results/deepseek/src/ds_e2e.py "$@"
