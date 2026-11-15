#!/usr/bin/env bash
set -euo pipefail

# Acquire the shared validation lock before creating the 10k fixture or starting
# a local application/browser. flock itself prevents a contended run from starting.
LOCK_PATH=/tmp/nonebot-project-validation.lock
PYTHON=/home/nonebot-shared-venv/bin/python

if [ "${1:-}" = "--pytest" ]; then
  shift
  exec flock -w 600 "$LOCK_PATH" "$PYTHON" -m pytest "$@"
fi

exec flock -w 600 "$LOCK_PATH" \
  "$PYTHON" "$(dirname "$0")/benchmark_performance.py" "$@"
