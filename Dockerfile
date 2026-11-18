# StockMonitor V3: web app (gunicorn) + scheduler in one container.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata \
    APP_BIND_HOST=0.0.0.0 \
    PORT=5002 \
    RUN_SCHEDULER=true

# tini: PID 1 that forwards stop signals to both processes.
# tzdata: IST market hours. git: optional GitHub Pages publishing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini tzdata git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh publish_report.sh \
    && mkdir -p data logs reports exports public \
    && chown -R app:app /app

VOLUME ["/app/data", "/app/logs", "/app/reports", "/app/exports"]
EXPOSE 5002

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','5002'), timeout=8)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker/entrypoint.sh"]
