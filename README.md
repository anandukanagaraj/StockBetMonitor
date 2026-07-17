# My Portfolio Bets V3

My Portfolio Bets V3 is a personal stock and options trading tracker.

It includes:

- V3 dashboard.
- Stock bet tracker.
- Pretty stock report from database.
- Masked public report.
- Target-visible private report.
- Option strategy tracker.
- Option what-if comparison.
- Portfolio review checklist.
- Scheduler for market-hours checks.
- WhatsApp alerts.
- End-of-day email.
- GitHub Pages publishing for masked report.
- Excel import and export.

This is a personal trading tracker. It is not stock advice, investment advice, or a buy/sell recommendation system.

## Documentation

Detailed docs are available in:

- `docs/FSD.md` - Functional Specification Document.
- `docs/TSD.md` - Technical Specification Document.
- `docs/HELP.md` - User help and operations guide.

## Project Folder

Default local path:

```powershell
C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V3
```

## Requirements

- Windows.
- Python 3.
- PowerShell.
- Git for publishing.
- Gmail app password for EOD email, if email is enabled.
- WhatsApp Cloud API credentials, if WhatsApp alerts are enabled.

Python packages:

```text
Flask
openpyxl
yfinance
requests
```

They are listed in `requirements.txt`.

## First-Time Setup

Open PowerShell:

```powershell
cd "C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V3"
py -m venv .venv
& ".\.venv\Scripts\pip.exe" install -r requirements.txt
```

## Start the Web App

Easy option:

```powershell
.\start_app.cmd
```

Or run Python directly:

```powershell
cd "C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V3"
& ".\.venv\Scripts\python.exe" app.py
```

Open:

```text
http://127.0.0.1:5002/
```

PowerShell note: use `&` before the Python executable path.

Correct:

```powershell
& ".\.venv\Scripts\python.exe" app.py
```

Incorrect:

```powershell
".\.venv\Scripts\python.exe" app.py
```

## Login

Default local login:

```text
Username: admin
Password: admin123
```

Before hosting or exposing beyond localhost, configure a secure password:

```powershell
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="your-strong-password"
& ".\.venv\Scripts\python.exe" app.py
```

Preferred hosted setup is `ADMIN_PASSWORD_HASH`.

## Main URLs

```text
Dashboard:              http://127.0.0.1:5002/
Masked stock report:    http://127.0.0.1:5002/report
Report with targets:    http://127.0.0.1:5002/report-targets
Add/manage stock bets:  http://127.0.0.1:5002/add-bet
Options dashboard:      http://127.0.0.1:5002/options
Add option bet:         http://127.0.0.1:5002/options/new
Portfolio review:       http://127.0.0.1:5002/index-membership
Export Excel:           http://127.0.0.1:5002/export/excel
```

## What V3 Adds Over V2

V2 focused on stock bets and pretty HTML reports.

V3 adds:

- A new `/` dashboard.
- Options strategy tracking.
- Option legs.
- Strategy-level P/L.
- What-if comparison:
  - Actual strategy.
  - Long Call Only.
  - Long Put Only.
  - Long Stock 10L.
- Approximate transaction charges.
- Options growth chart.
- Portfolio review checklist.

The stock report look and feel is preserved through `/report` and `/report-targets`.

## Stock Bets

Use:

```text
http://127.0.0.1:5002/add-bet
```

Required fields:

- Ticker.
- Buy date.
- Quantity.
- Buy price.
- Target price.

For closed stock bets:

- Sell date.
- Sell price.

Current price is auto-updated where possible.

## Option Bets

Use:

```text
http://127.0.0.1:5002/options/new
```

V3 tracks option bets at two levels:

- Strategy: parent trade idea.
- Legs: individual option contracts.

Supported strategy examples:

- Long Call.
- Long Put.
- Bull Call Spread.
- Bear Put Spread.
- Credit Spread.
- Debit Spread.
- Iron Condor.
- Straddle.
- Strangle.

Leg fields:

- BUY or SELL.
- Ticker.
- Quantity.
- Entry Premium.
- Exit Premium.

Terminology:

- Entry Premium is premium paid or received at entry.
- Exit Premium is premium received or paid at close.

For SELL legs, entry premium is the premium received and exit premium is the premium paid to close.

## Option What-If Charges

The option what-if section includes approximate charges:

- Brokerage.
- STT.
- Exchange transaction charges.
- SEBI charges.
- Stamp duty.
- GST.
- DP charges where applicable.

These are estimates and will not exactly match a broker contract note.

## Configuration

Main config file:

```text
config.ini
```

Important: keep real app passwords, WhatsApp access tokens, and phone IDs private. Do not commit or share real secrets.

### Market Timing

```ini
[MARKET]
START_TIME=09:15
END_TIME=15:30
REPORT_TIME=15:35
CHECK_INTERVAL_MINUTES=30
```

### Email

```ini
[EMAIL]
ENABLE_EMAIL=YES
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
EMAIL=your-email@gmail.com
APP_PASSWORD=your-gmail-app-password
TO_EMAIL=recipient@gmail.com
```

### WhatsApp

```ini
[WHATSAPP]
ENABLE_WHATSAPP=YES
ACCESS_TOKEN=your-whatsapp-token
PHONE_NUMBER_ID=your-phone-number-id
RECIPIENT=recipient-phone-number
```

### Publishing

```ini
[PUBLISH]
ENABLE_AFTER_EOD=YES
PUBLIC_DIR=C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V2\public
```

## Run Scheduler Manually

The scheduler:

- Runs price checks during Indian market hours.
- Updates current prices.
- Sends WhatsApp alerts if configured.
- Generates reports every market cycle.
- Sends EOD email after market close.
- Publishes masked report after successful EOD if enabled.
- Uses a single-instance lock so duplicate scheduler starts exit instead of sending duplicate alerts or publishing duplicate reports.

Run continuously:

```powershell
cd "C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V3"
& ".\.venv\Scripts\python.exe" scheduler.py
```

Run once:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once
```

Run once and force checks outside market hours:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once --force
```

## Install Scheduler Auto-Start

This installs the scheduler to start when you log in to Windows.

```powershell
cd "C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V3"
.\install_scheduler_task.ps1
```

If PowerShell blocks the script:

```powershell
powershell -ExecutionPolicy Bypass -File ".\install_scheduler_task.ps1"
```

Start manually:

```powershell
Start-ScheduledTask -TaskName "StockMonitorV3Scheduler"
```

Check status:

```powershell
Get-ScheduledTask -TaskName "StockMonitorV3Scheduler" | Get-ScheduledTaskInfo
```

The task name is `StockMonitorV3Scheduler` and should point to this V3 folder.

If Windows blocks scheduled-task registration, the installer creates a Startup-folder launcher instead:

```text
C:\Users\anand\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\StockMonitorV3Scheduler.cmd
```

That launcher starts the scheduler the next time you log in. The scheduler has a lock file, so a duplicate start exits instead of sending duplicate alerts or emails.

Manual scheduler launcher:

```powershell
.\start_scheduler.cmd
```

## Logs

Logs are written to:

```text
logs/YYYY-MM-DD.log
```

Tail today's log:

```powershell
Get-Content ".\logs\2026-07-17.log" -Tail 100
```

Search useful events:

```powershell
Select-String -Path ".\logs\2026-07-17.log" -Pattern "Scheduled market check","WhatsApp","EOD","Report generated","Published"
```

## EOD Email

The EOD process starts after `REPORT_TIME`, default `15:35`.

It sends once per day. The sent state is stored in:

```text
data/scheduler_state.json
```

To test EOD behavior:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once --force
```

## Publish Masked Report to GitHub Pages

Prerequisites:

- Git installed.
- `PUBLIC_DIR` in `config.ini` points to the GitHub Pages repository folder.
- At least one masked report exists in `reports/`.

Current setup publishes from V3 reports into:

```text
C:\Users\anand\OneDrive\Documents\Whatsapp Alerts\StockMonitor-V2\public
```

Generate a report by opening:

```text
http://127.0.0.1:5002/report
```

Publish:

```powershell
.\publish_report.ps1 -Message "Update report"
```

Commit without push:

```powershell
.\publish_report.ps1 -Message "Test publish" -NoPush
```

Published URL:

```text
https://anandukanagaraj.github.io/myportfoliobets-report/
```

## Excel Import

Default import:

```powershell
& ".\.venv\Scripts\python.exe" import_excel.py
```

Specific file:

```powershell
& ".\.venv\Scripts\python.exe" import_excel.py --excel "C:\path\to\Portfolio.xlsx"
```

## Excel Export

Open:

```text
http://127.0.0.1:5002/export/excel
```

Output folder:

```text
exports/
```

## Data Backup

Main database:

```text
data/stock_monitor.db
```

Back up before imports or large changes:

```powershell
Copy-Item ".\data\stock_monitor.db" ".\data\stock_monitor_backup_$(Get-Date -Format yyyyMMdd_HHmmss).db"
```

## Troubleshooting

### App does not start

Check virtual environment exists:

```powershell
Test-Path ".\.venv\Scripts\python.exe"
```

Install requirements:

```powershell
& ".\.venv\Scripts\pip.exe" install -r requirements.txt
```

### Port 5002 is already used

Find Python processes:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Select-Object ProcessId,CommandLine
```

Stop the relevant one:

```powershell
Stop-Process -Id <process-id> -Force
```

### Scheduler is not running

Check task:

```powershell
Get-ScheduledTask -TaskName "StockMonitorV3Scheduler" | Get-ScheduledTaskInfo
```

Check logs:

```powershell
Get-Content ".\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 100
```

### EOD email not received

Check:

- `ENABLE_EMAIL=YES`.
- Gmail app password is valid.
- Scheduler is running after `REPORT_TIME`.
- `data/scheduler_state.json` has not already marked today as sent.
- Logs contain `EOD email send started`.

### GitHub Pages shows 404

Check:

- Repository has `index.html`.
- GitHub Pages deployment completed.
- Correct branch is selected in GitHub Pages settings.
- Browser cache was refreshed.

## Security Checklist Before Hosting

- Replace default admin password.
- Use `ADMIN_PASSWORD_HASH`.
- Move secrets out of committed files.
- Keep target-visible report private.
- Use HTTPS on hosted deployments.
- Back up the database regularly.
