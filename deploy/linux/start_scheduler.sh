#!/usr/bin/env bash
# Run the scheduler in the foreground (manual testing). Production uses systemd.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export TZ="${TZ:-Asia/Kolkata}"
exec .venv/bin/python scheduler.py "$@"
