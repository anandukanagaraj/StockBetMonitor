# Deploying StockMonitor V3 with Docker

The whole app runs as **one container**: the gunicorn web app plus the
scheduler (price checks, alerts, EOD report). Code is baked into the image;
config, secrets and data live on the server and are mounted in.

| Server path (next to `docker-compose.yml`) | In container | Purpose |
|---|---|---|
| `.env` | environment | Secret key, admin password hash, API key |
| `config.ini` | `/app/config.ini` (read-only) | Market hours, holidays, alert settings |
| `data/` | `/app/data` | SQLite database, caches, scheduler heartbeat |
| `reports/`, `logs/`, `exports/` | same | Generated reports, logs, Excel exports |

The container runs in IST (`TZ=Asia/Kolkata`) whatever the server clock is.

## 1. Prerequisites

Docker Engine with the Compose plugin on the server (Linux, x86-64 or ARM64).

## 2. Get the code

```bash
git clone -b V3 https://github.com/anandukanagaraj/StockBetMonitor.git stockmonitor
cd stockmonitor
```

## 3. Configure

```bash
cp .env.example .env
cp config.ini.example config.ini
mkdir -p data logs reports exports
```

Fill in `.env`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"          # -> APP_SECRET_KEY
docker compose build
docker compose run --rm stockmonitor python docker/hash_password.py   # -> ADMIN_PASSWORD_HASH line
```

Then lock down the secrets so only you and the container user (uid 1000) can read them:

```bash
chmod 600 .env
sudo chown 1000:1000 config.ini && chmod 600 config.ini
```

The app **refuses to start** without `APP_SECRET_KEY` and `ADMIN_PASSWORD_HASH`.

`ADMIN_PASSWORD_HASH` only works for the admin's first login. After that the
password stored in the database is used. If you copy your laptop database to the
server, the laptop password comes with it. Accounts still on the default
`admin123` password are refused on a hosted server. To set a password directly:

```bash
docker compose exec stockmonitor python reset_password.py admin
```

### Avoid duplicate alerts

`config.ini.example` ships with email, Telegram and GitHub Pages publishing
**off**. Leave them off while your Windows scheduler is still running,
or every alert is sent twice. Turn them on only when this server is the only
scheduler.

## 4. Bring over existing data (optional)

Without this the app starts with an empty database. On Windows, stop the app and
scheduler first so the database is fully written, then copy
`data\stock_monitor.db` (and optionally `data\groww_stock_slugs.json`,
`data\index_membership_snapshot.json`) into the server's `data/` folder.

## 5. Start

```bash
docker compose up -d --build
docker compose ps                  # STATUS should become "healthy"
curl http://localhost:5002/health
docker compose logs -f
```

Open `http://<server>:5002/` and sign in with the admin user.

## Updating

```bash
git pull
docker compose up -d --build
```

Data in the mounted folders is untouched; database migrations run automatically
at start-up.

## HTTPS / reverse proxy

Put nginx, Caddy or Traefik in front, then set in `.env`:

```
SESSION_COOKIE_SECURE=true
TRUST_PROXY=true
```

and change the port mapping in `docker-compose.yml` to `"127.0.0.1:5002:5002"`
so the app is reachable only through the proxy.

## Options

| `.env` variable | Default | Effect |
|---|---|---|
| `RUN_SCHEDULER` | `true` | `false` runs the web app only |
| `HOST_PORT` | `5002` | Port published on the server |
| `GUNICORN_THREADS` | `8` | Web request threads (single worker, for SQLite) |

## Backups

Everything worth keeping is in `data/`. For a consistent copy while running:

```bash
docker compose exec stockmonitor python -c "import sqlite3; s=sqlite3.connect('data/stock_monitor.db'); d=sqlite3.connect('data/backup.db'); s.backup(d)"
```

## Publishing the report to GitHub Pages (optional)

Mount a clone of the Pages repository at `/app/public` (see the commented
volume in `docker-compose.yml`), give it push credentials (for example a
deploy key or token in its remote URL), and set `ENABLE_AFTER_EOD=YES` in
`config.ini`.
