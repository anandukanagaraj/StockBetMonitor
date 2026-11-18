# Technical Specification Document

# My Portfolio Bets V3

## 1. Overview

My Portfolio Bets V3 is a Flask-based local web application backed by SQLite. It tracks stock bets and option strategy bets, generates HTML stock reports, evaluates portfolio review rules, runs a market-hours scheduler, sends WhatsApp and email notifications, and can publish a masked report to GitHub Pages.

V3 was created as a copy of the V2 stock report application with option strategy tracking added on top. V2 stock report behavior remains available through dedicated report routes.

## 2. Technology Stack

- Python 3.
- Flask.
- SQLite.
- openpyxl.
- yfinance.
- requests.
- PowerShell scripts for scheduler installation and report publishing.
- GitHub Pages for static masked-report hosting.

Python dependencies are listed in `requirements.txt`.

## 3. Runtime Layout

Project root:

```text
StockMonitor-V3/
  app.py
  db.py
  options_db.py
  scheduler.py
  import_excel.py
  publish_report.ps1
  install_scheduler_task.ps1
  config.ini
  requirements.txt
  templates/
  static/
  services/
  data/
  reports/
  exports/
  logs/
  docs/
```

Generated folders:

- `data/`: SQLite database, scheduler state, index snapshot, yfinance cache.
- `reports/`: generated masked and target-visible HTML reports.
- `exports/`: generated Excel exports.
- `logs/`: app and scheduler daily log files.
- `public/`: local GitHub Pages repository, if configured.

## 4. Application Entry Point

File:

- `app.py`

Local startup:

```powershell
.\start_app.cmd
```

Direct Python startup:

```powershell
& ".\.venv\Scripts\python.exe" app.py
```

Default host:

```text
http://127.0.0.1:5002
```

Flask run configuration:

```python
app.run(host="127.0.0.1", debug=False, port=5002, use_reloader=False)
```

`APP_BIND_HOST`, `APP_PORT`, and `APP_SECRET_KEY` can be overridden with environment variables. The app refuses to start on non-localhost binds without `APP_SECRET_KEY` and `ADMIN_PASSWORD_HASH`.

Health endpoint:

```text
GET /health
```

Returns app, database, and scheduler heartbeat status. Scheduler heartbeat is written to `data/scheduler_heartbeat.json`.

Report retention:

- Old HTML snapshots are cleaned up automatically after report generation.
- Manual cleanup: `.\cleanup_reports.ps1 -KeepDays 14`
- Config key: `[APP] REPORT_KEEP_DAYS` (default `14`)

The reloader is disabled to avoid duplicate scheduler-like side effects during local development.

## 5. Web Routes

### Dashboard

```text
GET /
```

Protected by login. Builds a V3 dashboard from stock metrics, option metrics, portfolio review summary, and recent activity.

### Masked Stock Report

```text
GET /report
```

Generates and returns a masked stock report using `ReportGenerator`.

### Target-Visible Stock Report

```text
GET /report-targets
```

Generates and returns a stock report with target prices visible.

### Login and Logout

```text
GET  /login
POST /login
POST /logout
```

Authentication is session-based. Password verification uses Werkzeug password hashing. If `ADMIN_PASSWORD_HASH` is not set, the app generates a hash from `ADMIN_PASSWORD`, defaulting to `admin123`.

### Portfolio Review

```text
GET /index-membership
```

Builds portfolio review data using `services/index_service.py`.

### Options

```text
GET  /options
GET  /options/new
POST /options/new
GET  /options/<id>
GET  /options/<id>/edit
POST /options/<id>/edit
POST /options/<id>/delete
```

### Stock Bet Management

```text
GET  /add-bet
POST /add-bet
GET  /positions/new
POST /positions/new
GET  /positions/<id>/edit
POST /positions/<id>/edit
POST /positions/<id>/delete
POST /positions/<id>/refresh-price
```

### Excel Export

```text
GET /export/excel
```

Exports stock bets using `services/excel_exporter.py`.

## 6. Database Design

SQLite database path:

```text
data/stock_monitor.db
```

## 6.1 Stock Bets Table

Defined in:

- `db.py`

Table:

```text
positions
```

Columns:

- `id`
- `bet_no`
- `ticker`
- `buy_date`
- `qty`
- `buy_price`
- `target_price`
- `stop_loss`
- `current_price`
- `sell_date`
- `sell_price`
- `status`
- `notes`
- `is_test_data`
- `created_at`
- `updated_at`

Closed-position detection:

- A row is closed if `sell_price` exists, `sell_date` exists, or status is one of:
  - `closed`
  - `target hit`
  - `stop loss hit`
  - `sold`

## 6.2 Option Tables

Defined in:

- `options_db.py`

Tables:

```text
option_strategies
option_legs
schema_migrations
```

### option_strategies

Columns include:

- `id`
- `strategy_no`
- `bet_no`
- `strategy_type`
- `underlying`
- `buy_date`
- `sell_date`
- `expiry_date`
- `status`
- `margin`
- `max_risk`
- `max_profit`
- `strategy_pl`
- `pl_pct`
- `stock_move_pct`
- `long_only_pl`
- `notes`
- `is_test_data`
- `created_at`
- `updated_at`

### option_legs

Columns include:

- `id`
- `strategy_id`
- `leg_no`
- `action`
- `ticker`
- `underlying`
- `expiry_text`
- `strike`
- `option_type`
- `qty`
- `buy_price`
- `trade_capital`
- `sell_price`
- `achieved_pl`
- `status`
- `created_at`
- `updated_at`

Internal database fields still use `buy_price` and `sell_price`, but the UI displays them as Entry Premium and Exit Premium.

## 7. Option Calculation Logic

Implemented in:

- `options_db.py`
- `app.py`

### Leg P/L

BUY leg:

```text
(exit premium - entry premium) * qty
```

SELL leg:

```text
(entry premium - exit premium) * qty
```

### Strategy P/L

If strategy P/L is blank and at least one leg has an exit premium:

```text
sum(leg achieved P/L)
```

### Strategy P/L %

Current denominator:

```text
sum(entry premium * qty)
```

This is treated as trade capital for reporting. More advanced margin-based denominator support can be added later.

### Long Stock What-If

Long stock simulation uses:

```text
LONG_STOCK_NOTIONAL = 1,000,000
```

P/L:

```text
LONG_STOCK_NOTIONAL * stock_move_pct / 100
```

Stock move is fetched using `historical_stock_move_pct` in `services/yahoo_service.py`.

## 8. Approximate Charges Model

Implemented in:

- `app.py`

The option what-if summary uses approximate charges, not contract-note exact charges.

Current charge components:

- Brokerage.
- STT.
- Exchange transaction charges.
- SEBI charges.
- Stamp duty.
- GST.
- DP charges where applicable.

Important constants:

```python
BROKERAGE_PER_ORDER = 20
OPTION_STT_RATE = 0.0015
EQUITY_DELIVERY_STT_RATE = 0.001
OPTION_TRANSACTION_RATE = 0.0003553
EQUITY_DELIVERY_TRANSACTION_RATE = 0.0000307
SEBI_RATE = 10 / 10_000_000
OPTION_STAMP_RATE = 0.00003
EQUITY_DELIVERY_STAMP_RATE = 0.00015
GST_RATE = 0.18
EQUITY_DP_CHARGE = 15.34
```

These values should be reviewed periodically because broker and exchange charges can change.

## 9. Stock Report Generation

Implemented in:

- `services/v1_report_generator.py`
- `app.py`
- `scheduler.py`

The app converts database rows into V1-compatible position objects, then calls:

```python
ReportGenerator().generate_html(positions, show_target_price=...)
```

Output files:

```text
reports/report_masked_<timestamp>.html
reports/report_targets_<timestamp>.html
```

## 10. Portfolio Review / Index Service

Implemented in:

- `services/index_service.py`

Responsibilities:

- Fetch NSE index membership files.
- Cache membership data.
- Compare open stock bets with index membership.
- Calculate average winning hold days.
- Calculate open days.
- Identify mandatory sell-review triggers.
- Build summary cards and table rows.

Snapshot:

```text
data/index_membership_snapshot.json
```

## 11. Scheduler Design

File:

- `scheduler.py`

Configuration:

- `config.ini`
- `[SCHEDULER].STOCK_GROUP_CODES`, default `SWING`

State:

- `data/scheduler_state.json`

Single-instance lock:

- `data/scheduler.lock`

Logs:

- `logs/YYYY-MM-DD.log`

## 11.1 Scheduler Modes

Run forever:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py
```

Run one normal cycle:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once
```

Run one forced cycle outside market hours:

```powershell
& ".\.venv\Scripts\python.exe" scheduler.py --once --force
```

## 11.2 Market Cycle

During configured market hours:

1. Load open positions from configured scheduler stock groups.
2. Fetch latest price for each ticker.
3. Update `current_price`.
4. Evaluate target, near-target, very-close, and stop-loss conditions.
5. Send WhatsApp alerts if enabled and credentials are present.
6. Generate masked and target-visible reports.
7. Log all ticker-level evaluations.

The scheduler uses `scheduler_stock_group_codes(config)` and
`scheduler_positions(config)` to enforce the stock group allowlist. For now the
backend config is:

```ini
[SCHEDULER]
STOCK_GROUP_CODES=SWING
```

This same allowlist is used for market-cycle price refreshes, report snapshots,
EOD email content, and EOD publishing.

## 11.3 EOD Cycle

After `REPORT_TIME`, default `15:35`:

1. Check `data/scheduler_state.json`.
2. If EOD email was already sent today, skip.
3. Build EOD text and HTML content.
4. Send email if enabled.
5. Save sent state.
6. Generate fresh reports.
7. Publish masked report if enabled.

## 12. Scheduler Installation

Script:

- `install_scheduler_task.ps1`

The script first tries to install a Windows Scheduled Task that starts at user logon. If task registration fails, it creates a Startup folder launcher.

At runtime, `SchedulerInstanceLock` locks `data/scheduler.lock`. If another scheduler is already running, the new process logs an error and exits.

Default task name:

```text
StockMonitorV3Scheduler
```

The task name should point to the current V3 scheduler in the V3 folder.

## 13. Publishing Design

Script:

- `publish_report.ps1`

Steps:

1. Find latest `reports/report_masked_*.html`.
2. Copy it to `public/index.html`.
3. Run `git status`.
4. Commit if changed.
5. Push to origin main unless `-NoPush` is supplied.

Dependencies:

- Git must be installed.
- `public/` must be a local clone of the GitHub Pages repository.

## 14. Logging

Both Flask app and scheduler write to:

```text
logs/YYYY-MM-DD.log
```

Useful log entries:

- Request start.
- Form keys.
- Report generation.
- Price fetch and ticker details.
- WhatsApp previews or sends.
- EOD email flow.
- Publishing result.
- Exceptions.

## 15. Security Notes

- `config.ini` contains secrets and should remain local.
- Do not commit real app passwords, WhatsApp tokens, or phone IDs.
- Use environment variables for hosted deployments.
- Set `ADMIN_PASSWORD_HASH` before exposing the app beyond localhost.
- Set `APP_SECRET_KEY` before binding `APP_BIND_HOST` beyond localhost.
- Portfolio API accepts `X-API-Key` header only; non-local requests are rejected when the key is unset.
- CSRF validation is applied to POST requests.

## 16. Known Limitations

- Charge model is approximate.
- Option denominator currently uses trade capital, not broker margin.
- Index membership effective dates depend on available source data or cached checks.
- Scheduler runs only while the machine or server is running.
- The app is designed for single-user personal use.
