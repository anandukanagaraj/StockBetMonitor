# Help Guide

# My Portfolio Bets V3

## 1. Quick Start

Open PowerShell:

```powershell
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
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

Log in with your configured admin credentials.

If you have not configured credentials, local defaults are:

```text
Username: admin
Password: admin123
```

Change this before hosting.

## 2. Main Pages

### Dashboard

URL:

```text
http://127.0.0.1:5002/
```

Use this page for a quick overview:

- Combined realized P/L.
- Stock bet summary.
- Option bet summary.
- Portfolio review signals.
- Recent activity.

### Stock Report

Masked:

```text
http://127.0.0.1:5002/report
```

With targets:

```text
http://127.0.0.1:5002/report-targets
```

Use masked report for sharing or GitHub Pages publishing. Use target-visible report privately.

### Add or Manage Stock Bets

```text
http://127.0.0.1:5002/add-bet
```

Use this page to add, edit, refresh, or remove stock bets.

### Options Dashboard

```text
http://127.0.0.1:5002/options
```

Use this page to:

- View option strategy performance.
- Filter by strategy type.
- Expand rows to see option legs.
- View, edit, or remove eligible option bets.
- Review what-if comparisons.

### Add Option Bet

```text
http://127.0.0.1:5002/options/new
```

Pick a strategy type first. The app will prepare the expected number of leg rows.

Examples:

- Long Call: 1 leg.
- Bull Call Spread: 2 legs.
- Iron Condor: 4 legs.

### Portfolio Review

```text
http://127.0.0.1:5002/index-membership
```

Use this page to review underperforming stock bets based on:

- 75%+ fall.
- Above average winning hold days.
- Held 5 years or more.
- Index membership degradation.

## 3. Stock Bet Entry Rules

Required fields:

- Ticker.
- Buy date.
- Quantity.
- Buy price.
- Target price.

For closed stock bets:

- Sell date is required.
- Sell price is required.

Current price:

- The app tries to auto-fill current price after save.
- You can also refresh price from the stock management page.

Test data:

- Mark a row as test data if it is only for UI/report testing.
- Test data can be removed later.

## 4. Option Bet Entry Rules

Strategy-level fields:

- Strategy type.
- Underlying.
- Buy date.
- Sell date, if closed.
- Expiry date, optional.
- Status.

Leg-level fields:

- Action: BUY or SELL.
- Ticker.
- Quantity.
- Entry Premium.
- Exit Premium.

Premium meaning:

- BUY leg:
  - Entry Premium = premium paid.
  - Exit Premium = premium received.
- SELL leg:
  - Entry Premium = premium received.
  - Exit Premium = premium paid to close.

Closed strategies:

- Sell date is required.
- Exit premium is required for all legs.

## 5. Editing Option Bets

From `/options`:

- Click the eye icon to view details.
- Click the pencil icon to edit.
- Click the delete icon only for open bets or test data.

Closed real option bets do not show delete by default.

## 6. Understanding Option What-If

The option dashboard compares:

- Actual strategy.
- Long Call Only.
- Long Put Only.
- Long Stock 10L.

The purpose is to answer:

```text
Was the spread or strategy better than simply buying the long option leg?
Was it better than taking a long stock position?
```

Approximate charges include:

- Brokerage.
- STT.
- Exchange transaction charges.
- SEBI charges.
- Stamp duty.
- GST.
- DP charges where applicable.

These charges are approximate and may not match broker contract notes exactly.

## 7. Running the Scheduler

The scheduler handles:

- Market-hours price updates.
- WhatsApp alerts.
- Report generation.
- EOD email.
- Optional publishing after EOD.
- Single-instance locking to prevent duplicate scheduler processes.

Run continuously:

```powershell
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
& ".\.venv\Scripts\python.exe" scheduler.py
```

Run one normal cycle:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once
```

Run one forced cycle:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once --force
```

Use `--force` when testing outside market hours.

## 8. Installing Scheduler Auto-Start

Run PowerShell:

```powershell
cd "C:\Anand\VSS_Workspace\StockMonitor-V3"
.\install_scheduler_task.ps1
```

If PowerShell blocks scripts:

```powershell
powershell -ExecutionPolicy Bypass -File ".\install_scheduler_task.ps1"
```

Start the task manually:

```powershell
Start-ScheduledTask -TaskName "StockMonitorV3Scheduler"
```

Check task status:

```powershell
Get-ScheduledTask -TaskName "StockMonitorV3Scheduler" | Get-ScheduledTaskInfo
```

The task name is `StockMonitorV3Scheduler` and should point to this V3 folder.

Manual scheduler launcher:

```powershell
.\start_scheduler.cmd
```

## 9. Scheduler Configuration

Configuration file:

```text
config.ini
```

Market section:

```ini
[MARKET]
START_TIME=09:15
END_TIME=15:30
REPORT_TIME=15:35
CHECK_INTERVAL_MINUTES=30
```

Scheduler scope:

```ini
[SCHEDULER]
STOCK_GROUP_CODES=SWING
```

Only the stock groups listed here are used for scheduled price updates, alerts,
EOD email, generated stock reports, and publishing. For now this should stay as
`SWING`.

Email section:

```ini
[EMAIL]
ENABLE_EMAIL=YES
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
EMAIL=your-email@gmail.com
APP_PASSWORD=your-app-password
TO_EMAIL=recipient@gmail.com
```

WhatsApp section:

```ini
[WHATSAPP]
ENABLE_WHATSAPP=YES
ACCESS_TOKEN=your-token
PHONE_NUMBER_ID=your-phone-number-id
RECIPIENT=recipient-phone-number
```

Publishing section:

```ini
[PUBLISH]
ENABLE_AFTER_EOD=YES
PUBLIC_DIR=C:\Anand\VSS_Workspace\Whatsapp Alerts\StockMonitor-V2\public
```

Keep real secrets local.

## 10. Where Logs Are Stored

Logs are stored here:

```text
logs/YYYY-MM-DD.log
```

Example:

```powershell
Get-Content ".\logs\2026-07-17.log" -Tail 80
```

Useful things to search:

```powershell
Select-String -Path ".\logs\2026-07-17.log" -Pattern "EOD","WhatsApp","Scheduled market check","Report generated"
```

## 11. EOD Email Debug Checklist

If you did not receive EOD email:

1. Confirm scheduler is running.
2. Check current time is after `REPORT_TIME`.
3. Check `config.ini` has `ENABLE_EMAIL=YES`.
4. Check Gmail app password is valid.
5. Check log file for `EOD email send started`.
6. Check `data/scheduler_state.json` for `eod_email_sent_date`.
7. Check `PUBLIC_DIR` points to the GitHub Pages repo if publish did not happen.
8. Use forced test:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once --force
```

## 12. Publishing Masked Report

The publish script copies the latest V3 masked report into the GitHub Pages repo configured by:

```ini
[PUBLISH]
PUBLIC_DIR=C:\Anand\VSS_Workspace\Whatsapp Alerts\StockMonitor-V2\public
```

Generate a report first by opening:

```text
http://127.0.0.1:5002/report
```

Then publish:

```powershell
.\publish_report.ps1 -Message "Update report"
```

Commit without pushing:

```powershell
.\publish_report.ps1 -Message "Test publish" -NoPush
```

Published report URL:

```text
https://anandukanagaraj.github.io/myportfoliobets-report/
```

## 13. Exporting to Excel

Open:

```text
http://127.0.0.1:5002/export/excel
```

The export file is generated under:

```text
exports/
```

It uses the original source template layout where available.

## 14. Importing from Excel

Default import:

```powershell
& ".\.venv\Scripts\python.exe" import_excel.py
```

Import a specific file:

```powershell
& ".\.venv\Scripts\python.exe" import_excel.py --excel "C:\path\to\Portfolio.xlsx"
```

## 15. Common Problems

### PowerShell says Python command is invalid

Use the call operator `&`:

```powershell
& ".\.venv\Scripts\python.exe" app.py
```

### Port is already in use

Find Python processes:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Select-Object ProcessId,CommandLine
```

Stop the relevant process:

```powershell
Stop-Process -Id <process-id> -Force
```

### Git is not found while publishing

Install Git for Windows or add Git to PATH. The publish script also checks common Git installation paths.

### Report does not update on GitHub Pages

Check:

- `public/index.html` changed.
- Git commit happened.
- Git push succeeded.
- GitHub Pages deployment completed.
- Browser cache was refreshed.

## 16. Safety Notes

- This app is a personal trading tracker.
- It is not investment advice.
- Do not share target-visible report publicly.
- Do not commit `config.ini` with real secrets.
- Back up `data/stock_monitor.db` before large imports or migrations.
