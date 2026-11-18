#!/usr/bin/env bash
# Run the web app in the foreground (manual testing). Production uses systemd.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export TZ="${TZ:-Asia/Kolkata}"
exec .venv/bin/gunicorn --workers 1 --threads 8 --timeout 180 \
  --bind "${APP_BIND_HOST:-127.0.0.1}:${PORT:-5002}" app:app
