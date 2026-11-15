#!/usr/bin/env bash
set -euo pipefail

# Acquire the shared validation lock before creating the 10k fixture or starting
# a local application/browser. flock itself prevents a contended run from starting.
exec flock -w 600 /tmp/nonebot-project-validation.lock \
  /home/nonebot-shared-venv/bin/python "$(dirname "$0")/benchmark_performance.py" "$@"
