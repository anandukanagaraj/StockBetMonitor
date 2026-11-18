# Deploying StockMonitor V3 to a Linux (E2E) server

Tested layout: the app in `/opt/stockmonitor`, running as user `stockmonitor`,
with two systemd services: `stockmonitor-web` (gunicorn) and
`stockmonitor-scheduler`.

## 1. Install prerequisites

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv git
sudo useradd --system --create-home --home-dir /opt/stockmonitor stockmonitor
```

## 2. Get the code

```bash
sudo -u stockmonitor git clone -b V3 https://github.com/anandukanagaraj/StockBetMonitor.git /opt/stockmonitor
cd /opt/stockmonitor
sudo -u stockmonitor python3 -m venv .venv
sudo -u stockmonitor .venv/bin/pip install -r requirements.txt
chmod +x publish_report.sh deploy/linux/*.sh
```

## 3. Copy config and data (not in git)

From the Windows machine, copy these by hand (scp, WinSCP), never through git:

| File | Notes |
|------|-------|
| `config.ini` | Holds the Gmail app password, Telegram token and API key. `chmod 600`. |
| `data/stock_monitor.db` | Optional. Without it the server starts with an empty database. Stop the local app and scheduler before copying so the WAL is checkpointed. |

**Avoid duplicate alerts.** If the Windows scheduler keeps running, set these
in the server's `config.ini` so both machines don't email, message and publish:

```ini
[EMAIL]
ENABLE_EMAIL=NO
[TELEGRAM]
ENABLE_TELEGRAM=NO
[PUBLISH]
ENABLE_AFTER_EOD=NO
```

If publishing stays on, set `PUBLIC_DIR` to a Linux path (a clone of the GitHub
Pages repo with push access), or leave it unset to use `/opt/stockmonitor/public`.

## 4. Environment

```bash
sudo mkdir -p /etc/stockmonitor
sudo cp deploy/linux/stockmonitor.env.example /etc/stockmonitor/stockmonitor.env
sudo chmod 600 /etc/stockmonitor/stockmonitor.env
sudo nano /etc/stockmonitor/stockmonitor.env   # fill APP_SECRET_KEY, ADMIN_PASSWORD_HASH, PORTFOLIO_API_KEY
```

`TZ=Asia/Kolkata` in that file keeps the market-hours schedule in IST, whatever
the server clock is set to. The scheduler logs a warning at startup if it isn't IST.

## 5. Start the services

```bash
sudo cp deploy/linux/stockmonitor-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stockmonitor-web stockmonitor-scheduler
systemctl status stockmonitor-web stockmonitor-scheduler
```

Logs: `journalctl -u stockmonitor-web -f`, and `logs/` under `/opt/stockmonitor`.

## Updating

```bash
cd /opt/stockmonitor && sudo -u stockmonitor git pull
sudo -u stockmonitor .venv/bin/pip install -r requirements.txt
sudo systemctl restart stockmonitor-web stockmonitor-scheduler
```

## Manual run (without systemd)

```bash
deploy/linux/start_app.sh          # web app on 127.0.0.1:5002
deploy/linux/start_scheduler.sh --once --force
```
