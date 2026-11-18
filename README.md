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
C:\Anand\VSS_Workspace\StockMonitor-V3
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
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
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
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
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
Actual P/L API:         http://127.0.0.1:5002/api/portfolio/actual-pl
```

## PortfolioManager Actual P/L API

Use this endpoint from PortfolioManager to read actual stock and option P/L:

```text
GET http://127.0.0.1:5002/api/portfolio/actual-pl
```

For summary only:

```text
GET http://127.0.0.1:5002/api/portfolio/actual-pl?details=0
```

The response includes:

- `combined.realized_pl`
- `combined.actual_pl`
- `stocks.summary.realized_pl`
- `stocks.summary.unrealized_pl`
- `options.summary.realized_pl`
- `year_wise` keyed by year with `stock_realized_pl` and `option_realized_pl`
- optional `stocks.bets` and `options.bets` row-level details

Example yearly data:

```json
{
  "year_wise": {
    "2026": {
      "stock_realized_pl": 24050.02,
      "option_realized_pl": 52254.75
    }
  }
}
```

Optional API key protection:

```powershell
$env:PORTFOLIO_API_KEY="your-local-api-key"
```

Then send:

```text
X-API-Key: your-local-api-key
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

### Scheduler Scope

```ini
[SCHEDULER]
STOCK_GROUP_CODES=SWING
PRICE_FETCH_WORKERS=8
```

Only stock groups listed here are used by the scheduler for price refreshes,
WhatsApp alerts, generated stock reports, EOD email, and EOD publishing. Keep it
as `SWING` unless you intentionally want another stock group automated.
The scheduler first requests all unique symbols through Yahoo's bulk download,
then uses up to `PRICE_FETCH_WORKERS` parallel requests only for symbols missing
from the bulk response. Values are limited to 1-16; use `1` for sequential
fallback requests.

### Performance Controls

```ini
[OPTIONS]
WHATIF_TRADE_LIMIT=100
STOCK_MOVE_BACKFILL_LIMIT=5
```

`WHATIF_TRADE_LIMIT` caps the detailed simulated trade rows shown in the
Options What-if report. `STOCK_MOVE_BACKFILL_LIMIT` caps how many missing option
stock-move values the app tries to fetch during one page load. Keep this small
so Options pages stay responsive as data grows; the missing values will continue
to fill gradually across later loads.

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

WhatsApp is the primary alert channel. If it is disabled, incompletely configured,
or its API request fails, the scheduler sends the alert through both Telegram and
email instead.

### Telegram

```ini
[TELEGRAM]
ENABLE_TELEGRAM=YES
BOT_TOKEN=your-telegram-bot-token
CHAT_ID=your-telegram-chat-id
```

### Publishing

```ini
[PUBLISH]
ENABLE_AFTER_EOD=YES
PUBLIC_DIR=C:\Anand\VSS_Workspace\Whatsapp Alerts\StockMonitor-V2\public
```

## Run Scheduler Manually

The scheduler:

- Runs price checks during Indian market hours.
- Updates current prices for configured stock groups only.
- Sends WhatsApp alerts if configured.
- Generates reports every market cycle for configured stock groups only.
- Sends EOD email after market close.
- Publishes masked report after successful EOD if enabled.
- Uses a single-instance lock so duplicate scheduler starts exit instead of sending duplicate alerts or publishing duplicate reports.

Run continuously:

```powershell
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
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
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
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

Before sending the EOD email, the scheduler refreshes current prices for the
enabled stock groups. After a successful EOD email, it regenerates the masked and
target report snapshots and publishes the masked report when publishing is
enabled.

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
C:\Anand\VSS_Workspace\StockMonitor-V3\public
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

### Portfolio Analytics stock activity

The stock-only Bet Activity section supports ISO-weekly, monthly, calendar-quarter
and yearly views. Opened counts/capital use Buy Date; closed counts, proceeds and
realized P/L use Sell Date. Selecting a summary period drills into its events.
Detail search, event filters, numeric/date sorting, and 15/25/50/100-row pagination
do not change summary calculations. Options are excluded.

### Funding Sustainability report controls

The scenario comparison supports exact-value numeric sorting, including metrics
shown as secondary text through its Sort metric selector. The event ledger has
ticker/bet search, buy/exit and fresh/recycled-funding filters, sortable headers,
and 25/50/100-row pages. Both tables have ascending/descending controls and
independent pagination. Sorting never recalculates historical cash balances;
filters affect detail rows only, not scenario totals or charts.

### Profit Booking Simulation: −15% stop comparison

Open bets purchased today with a valid stored current price are included in both
stop scenarios at current MTM, even without post-purchase daily history. A
dedicated table shows the latest price relative to each stop and the recorded
target. Earlier intraday touches remain unverified; this does not fabricate a
realized exit or change the actual bet. Missing prices and older missing-history
bets are still reported as unavailable.

The stop dashboard now compares both −15% (85% of entry) and −75% (25% of
entry) using the same OHLC history. Each scenario shows hit count, realized P/L
from stop exits only, other closed-bet realized P/L, total realized P/L, remaining
open MTM, invested capital at purchase cost, and cash proceeds released by stops.
The two searchable, sortable, paginated stop-exit ledgers default to hit bets.
Profit-target analysis remains available separately in an expandable section.

The Live Report-style comparison separates realized P/L, open unrealized P/L,
combined P/L, open/closed counts, and remaining open capital at cost. Actual-all
totals remain visible next to the matched historical-data cohort. A simulated
stop on an actually open bet moves that bet to simulated closed/realized, with
zero remaining unrealized P/L. Open and Closed Bets tables are grouped by actual
status, show simulated status alongside it, and offer independent sorting,
ticker search, stop/exclusion filters, and 15/25/50/100-row pagination.
Filtering detail tables does not change summary totals.

The stock-only Profit Booking Simulation includes a fixed 15% stop overlay
against actual realized P/L for closed bets and current MTM for open bets.
If a daily Low touches 85% of the purchase price, the simulated exit is the stop
price, or the daily Open when it gaps below the stop. Otherwise the actual result
is retained. Stops start the day after purchase; on an actual sale date, a stop
touch is assumed to precede the recorded exit. No additional profit target,
re-entry, reinvestment, fees or corporate-action adjustments are modelled.
Missing OHLC/actual valuations are excluded from both comparison totals and
shown explicitly. Existing profit-target comparisons remain separate.

Historical quotes are prefetched with six workers; database and cache-file writes
remain on the calling thread. Historical ranges are cached, today's ranges expire
after five minutes, and repeated rendering no longer appends to cached price lists.
The first visit after the OHLC cache upgrade may be slower. A data-only check on
4 September 2026 took 8.05 seconds initially and 0.21 seconds on a repeat load;
this excludes browser rendering and is not a guaranteed response time.

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

The web application and scheduler are separate processes. Starting `app.py` does
not start `scheduler.py`.

1. Check whether the V3 scheduler process is running. The full command line is
important because another StockMonitor checkout may also have a scheduler:

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match "python" -and $_.CommandLine -match "StockMonitor-V3.*scheduler\.py" } |
    Select-Object ProcessId, CreationDate, CommandLine
```

2. Check the Windows task state and last result:

```powershell
$Task = Get-ScheduledTask -TaskName "StockMonitorV3Scheduler"
$Info = $Task | Get-ScheduledTaskInfo
$Task | Select-Object TaskName, State
$Info | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

Useful result codes:

- `0x00041301`: the task is currently running.
- `0xC000013A`: the scheduler was interrupted or terminated, for example when
  its console was closed or Windows stopped the process.
- A blank `NextRunTime` is normal for an at-logon trigger.

3. Confirm that the task action points to this repository, not an older checkout:

```powershell
(Get-ScheduledTask -TaskName "StockMonitorV3Scheduler").Actions |
    Select-Object Execute, Arguments, WorkingDirectory
```

The expected working directory is
`C:\Anand\VSS_Workspace\StockMonitor-V3`.

4. Inspect today's log for startup, locking, or Python errors:

```powershell
Get-Content ".\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 100
```

A healthy startup contains both `Scheduler instance lock acquired` and
`Scheduler started`. If the lock cannot be acquired, another V3 scheduler is
already running; do not start another copy.

5. Start the installed task and check it again:

```powershell
Start-ScheduledTask -TaskName "StockMonitorV3Scheduler"
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName "StockMonitorV3Scheduler" | Get-ScheduledTaskInfo
```

Alternatively, run the scheduler in the foreground to see errors immediately:

```powershell
.\start_scheduler.cmd
```

6. If automatic startup is missing or has the wrong path, reinstall it from an
elevated PowerShell window (Run as Administrator):

```powershell
powershell -ExecutionPolicy Bypass -File ".\install_scheduler_task.ps1"
```

Administrator rights may be required to replace an existing scheduled task.
The installer otherwise creates this per-user Startup-folder fallback:

```powershell
$StartupLauncher = Join-Path ([Environment]::GetFolderPath("Startup")) "StockMonitorV3Scheduler.cmd"
Test-Path $StartupLauncher
Get-Content $StartupLauncher
```

The fallback runs at the next Windows login. To use it immediately:

```powershell
& $StartupLauncher
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
- Set `APP_SECRET_KEY` when binding beyond `127.0.0.1`.
- Set `PORTFOLIO_API_KEY` for the portfolio API (`X-API-Key` header only).
- Move secrets out of committed files.
- Keep target-visible report private.
- Use HTTPS on hosted deployments.
- Back up the database regularly.
- Run `.\cleanup_reports.ps1` periodically if scheduler cleanup is disabled.

## Tests

```powershell
& ".\.venv\Scripts\python.exe" -m pytest
```
# NSE market holidays

The scheduler skips weekends and every date listed in `MARKET.HOLIDAYS` in
`config.ini`. Keep the value as a comma-separated list of ISO dates
(`YYYY-MM-DD`) and update it annually from the official NSE trading-holiday
circular. The holiday guard applies to intraday price checks, alerts, EOD
processing, and calculation of the next scheduler wake-up.
