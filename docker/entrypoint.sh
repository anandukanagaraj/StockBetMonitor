#!/usr/bin/env bash
# Starts the scheduler (restarted if it exits) and the gunicorn web app in one
# container. Runs first as root to fix volume ownership, then drops to "app".
set -euo pipefail
cd /app

if [[ "$(id -u)" == "0" ]]; then
  mkdir -p data logs reports exports
  chown app:app data logs reports exports
  exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

# Anything passed to the container (e.g. "python docker/hash_password.py")
# runs instead of the app.
if [[ $# -gt 0 ]]; then
  exec "$@"
fi

if [[ ! -f config.ini ]]; then
  echo "WARNING: /app/config.ini is not mounted; using built-in defaults (no email/Telegram alerts)." >&2
fi

# Run all database migrations once, before the web app and scheduler start,
# so they never race to create tables. Also fails fast on missing secrets.
echo "Initialising database..."
python -c "import app"

if [[ "${RUN_SCHEDULER,,}" == "true" ]]; then
  (
    while true; do
      python scheduler.py || true
      echo "Scheduler exited; restarting in 30s." >&2
      sleep 30
    done
  ) &
  echo "Scheduler started in background."
else
  echo "RUN_SCHEDULER is not true; web app only."
fi

exec gunicorn \
  --workers 1 \
  --threads "${GUNICORN_THREADS:-8}" \
  --timeout "${GUNICORN_TIMEOUT:-180}" \
  --bind "0.0.0.0:${PORT}" \
  --access-logfile - \
  app:app
