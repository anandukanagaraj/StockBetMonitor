# Functional Specification Document

# My Portfolio Bets V3

## 1. Purpose

My Portfolio Bets V3 is a local portfolio tracking platform for personal stock bets and option strategy bets. It extends the V2 stock-bet workflow with a V3 dashboard, options strategy tracking, portfolio review checks, scheduled price monitoring, WhatsApp alerts, end-of-day email reporting, and static masked-report publishing.

The application is intended as a personal trading tracker. It is not a stock recommendation system, investment advisory platform, or public tips service.

## 2. User Goals

- Track open and closed stock bets.
- Track option strategies with one or more legs.
- Compare actual option strategy results against what-if alternatives.
- Monitor stock bets during Indian market hours.
- Send WhatsApp alerts when price conditions are met.
- Send an end-of-day email summary after market close.
- Publish a masked stock report to GitHub Pages.
- Review underperforming stock bets using objective exit-review rules.
- Export stock bets back into the original Excel template format.

## 3. User Roles

### Admin User

The admin user can:

- Log in to the web app.
- View the V3 dashboard.
- Add, edit, and manage stock bets.
- Add, edit, view, and manage option bets.
- View masked and target-visible stock reports.
- View the portfolio review page.
- Export stock bets to Excel.
- Run or install the scheduler locally.

There is no multi-user role model in V3.

## 4. Major Functional Areas

## 4.1 V3 Dashboard

URL: `/`

The dashboard is the main landing page after login.

It shows:

- Combined realized P/L from stock and option bets.
- Stock bet count, open count, closed count.
- Option bet count, open count, closed count.
- Mandatory portfolio review count.
- Stock snapshot:
  - Open capital.
  - Realized P/L.
  - Wins / losses.
  - Win rate with denominator.
  - Average realized return.
- Option snapshot:
  - Realized P/L.
  - Average strategy return.
  - Wins / losses.
  - Outcome rate.
- Portfolio review signals:
  - Down 75%+.
  - Above average winning hold days.
  - Held 5 years or more.
  - Index membership degraded.
- Recent stock and option activity.

Expected behavior:

- If the user is not logged in, `/` redirects to login.
- Dashboard values are read from SQLite.
- Dashboard should be compact and avoid horizontal scroll.

## 4.2 Authentication

URL: `/login`

The app has a simple admin login.

Default local credentials:

- Username: `admin`
- Password: from `ADMIN_PASSWORD`, or `admin123` if no environment variable is set.

Production or hosted use must configure a secure password hash using `ADMIN_PASSWORD_HASH`.

Expected behavior:

- Login creates a session.
- Logout clears the session.
- Protected pages redirect unauthenticated users to login.

## 4.3 Stock Bets

Primary URLs:

- Add/manage stock bet: `/add-bet`
- Edit stock bet: `/positions/<id>/edit`
- Refresh stock price: `/positions/<id>/refresh-price`
- Delete stock bet: `/positions/<id>/delete`

Fields captured:

- Bet no.
- Ticker.
- Buy date.
- Quantity.
- Buy price.
- Current price.
- Target price.
- Stop loss.
- Sell date.
- Sell price.
- Status.
- Test-data flag.

Rules:

- Bet no is auto-generated.
- Buy date defaults to today for new stock bets.
- Target price is required.
- Closed bets require sell date and sell price.
- Current price is auto-updated after saving when possible.
- Closed real positions are not intended for routine deletion.
- Test data can be marked and removed during testing.

## 4.4 Stock Reports

URLs:

- Masked stock report: `/report`
- Target-visible stock report: `/report-targets`

The reports preserve the V1/V2 pretty report look and feel while sourcing data from the database.

Masked report:

- Masks target values.
- Intended for publishing or sharing.

Target-visible report:

- Shows target price.
- Intended for private use.

Report contents include:

- Header summary.
- Realized profit chart.
- Open and closed bets.
- Winners and losers.
- Near-target section.
- Disclaimer.

## 4.5 Portfolio Review

URL: `/index-membership`

Although the route name is still `index-membership`, the page functions as a portfolio review checklist.

It evaluates open stock bets using:

- Index membership status.
- Price fall from buy price.
- Days held compared with average winning hold days.
- Five-year hold rule.

Mandatory sell-review triggers:

- Price is down 75% or more.
- Open days exceed average winning hold days.
- Position is held for 5 years or more.
- Stock has been removed/degraded from index membership.

Expected page behavior:

- Show summary cards.
- Show sortable and paginated checklist.
- Avoid horizontal scroll where possible.
- Show last membership check date.

Index data source:

- NSE official constituent CSV files where available.
- Cached snapshot stored in `data/index_membership_snapshot.json`.

## 4.6 Option Bets

Primary URLs:

- Options dashboard: `/options`
- Add option strategy: `/options/new`
- Edit option strategy: `/options/<id>/edit`
- View option strategy detail: `/options/<id>`
- Delete option strategy: `/options/<id>/delete`

Option bet model:

- Option strategy is the parent record.
- Option legs are child records.

Supported strategy types:

- Long Call.
- Long Put.
- Bull Call Spread.
- Bear Put Spread.
- Bull Put Spread.
- Bear Call Spread.
- Debit Spread.
- Credit Spread.
- Iron Condor.
- Iron Butterfly.
- Straddle.
- Strangle.
- Custom Multi-leg.

Captured strategy fields:

- Option bet no.
- Strategy type.
- Underlying.
- Buy date.
- Sell date.
- Expiry date.
- Status.
- Test-data flag.
- Notes.

Captured leg fields:

- Action: BUY or SELL.
- Option ticker.
- Quantity.
- Entry premium.
- Exit premium.

Terminology:

- Entry Premium means premium paid or received at trade entry.
- Exit Premium means premium received or paid at close.
- For a BUY leg, entry premium is paid and exit premium is received.
- For a SELL leg, entry premium is received and exit premium is paid to close.

Option validations:

- Strategy must have at least one leg.
- Closed strategies require sell date.
- Closed strategies require exit premium for each leg.
- Entry premium is required for each leg.
- SELL legs are allowed and are calculated using option-specific logic.

## 4.7 Option Dashboard

URL: `/options`

Features:

- Summary metrics for option bets.
- Strategy filter.
- Growth chart.
- What-if summary.
- Sortable and paginated option bet list.
- Expand/collapse option legs per strategy.
- Compact icon actions for view and edit.
- Remove action only for open option bets or test data.

What-if comparisons:

- Actual strategy result.
- Long Call Only.
- Long Put Only.
- Long Stock 10L.

Costs:

The what-if summary includes approximate charges, not just brokerage.

Approximate charge stack:

- Brokerage.
- STT.
- Exchange transaction charges.
- SEBI charges.
- Stamp duty.
- GST.
- DP charges where applicable.

These values are estimates and should not be treated as broker contract-note exact values.

## 4.8 Scheduler

Script: `scheduler.py`

The scheduler is intended to run on the laptop or server.

During Indian market hours it:

- Reads open stock bets from SQLite.
- Fetches latest prices using Yahoo Finance.
- Updates current prices in the database.
- Evaluates target, near-target, and stop-loss conditions.
- Sends WhatsApp alerts when enabled.
- Generates masked and target-visible stock reports every cycle.

After market close it:

- Sends end-of-day email once per day.
- Generates fresh report snapshots.
- Publishes masked report to GitHub Pages if enabled.

Default schedule:

- Market start: `09:15`.
- Market end: `15:30`.
- EOD report time: `15:35`.
- Market check interval: `30` minutes.

## 4.9 WhatsApp Alerts

WhatsApp alerts are configured in `config.ini`.

Alerts include:

- Target hit.
- Very close to target.
- Near target.
- Stop loss, if enabled.

If WhatsApp credentials are missing, the app logs message previews instead of sending.

## 4.10 End-of-Day Email

The EOD email includes:

- Portfolio summary.
- Today's activities.
- New bets today.
- Closed bets today.
- Near target or target-related activity.
- Links to masked and target-visible reports.

Email is sent only once per day unless state is reset or the scheduler is forced.

State file:

- `data/scheduler_state.json`

## 4.11 Static Report Publishing

Script:

- `publish_report.ps1`

Purpose:

- Copy latest masked HTML report from `reports/`.
- Replace `public/index.html`.
- Commit and push the GitHub Pages repository.

Expected GitHub Pages URL:

- `https://anandukanagaraj.github.io/myportfoliobets-report/`

## 4.12 Excel Import and Export

Import script:

- `import_excel.py`

Export route:

- `/export/excel`

Export output:

- `exports/portfolio_export_<timestamp>.xlsx`

The export uses the original Excel template layout where available.

## 5. Non-Functional Requirements

## 5.1 Usability

- Avoid horizontal scroll where possible.
- Use compact icon actions for repeated row operations.
- Keep cards readable with large rupee values.
- Show denominators for rates, especially win rate and outcome rate.
- Use clear stock/options terminology.

## 5.2 Security

- App password and API tokens must not be committed or shared.
- Use `ADMIN_PASSWORD_HASH` before hosting.
- Login is required for operational pages.
- CSRF protection is enabled for POST actions.

## 5.3 Reliability

- Scheduler logs to daily log files.
- EOD email send state is persisted.
- Index membership snapshot is cached.
- Report publishing is triggered only after successful EOD flow.

## 5.4 Maintainability

- V3 builds on V2 instead of replacing it.
- Stock report generator is retained.
- Option tracking uses separate database tables.
- Reports and dashboards source data from SQLite.

