import configparser
import json
import logging
import msvcrt
import os
import smtplib
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from types import SimpleNamespace

import requests

from db import is_closed, list_positions, update_current_price
from services.date_utils import (
    display_date as display_shared_date,
    normalized_buy_date_map,
    parse_date as parse_shared_date,
    position_identity,
)
from services.v1_report_generator import ReportGenerator
from services.yahoo_service import latest_price


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = BASE_DIR / "data" / "scheduler_state.json"
LOCK_PATH = BASE_DIR / "data" / "scheduler.lock"


@dataclass
class AlertResult:
    ticker: str
    bet_no: int
    current_price: float
    target_price: float
    stop_loss: float | None
    buy_price: float
    qty: float
    profit: float
    profit_percent: float
    remaining: float
    remaining_percent: float
    status: str


def setup_logger():
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    logger = logging.getLogger("StockMonitorV3Scheduler")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = setup_logger()


class SchedulerInstanceLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(exist_ok=True)
        self.handle = self.path.open("a+")
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close()
            self.handle = None
            logger.error("Scheduler is already running. Lock file is held: %s", self.path)
            return False

        self.handle.seek(1)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()}\nstarted_at={datetime.now().isoformat(timespec='seconds')}\n")
        self.handle.flush()
        logger.info("Scheduler instance lock acquired: %s", self.path)
        return True

    def release(self):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            self.handle.truncate()
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            logger.info("Scheduler instance lock released: %s", self.path)
        finally:
            self.handle.close()
            self.handle = None


def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config


def get_bool(config, section, key, default=False):
    try:
        return config.getboolean(section, key)
    except Exception:
        return default


def parse_hhmm(value):
    return datetime.strptime(value, "%H:%M").time()


def today_at(value):
    return datetime.combine(datetime.now().date(), value)


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read scheduler state")
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def open_positions():
    return [row for row in list_positions() if not is_closed(row)]


def evaluate(row):
    current = row["current_price"]
    target = row["target_price"]
    buy = row["buy_price"]
    qty = row["qty"] or 0
    stop = row["stop_loss"]

    if current is None or target is None or buy is None:
        return None

    profit = (current - buy) * qty
    profit_percent = 0 if buy == 0 else ((current - buy) / buy) * 100
    remaining = target - current
    remaining_percent = 0 if target == 0 else (remaining / target) * 100

    if current >= target:
        status = "TARGET HIT"
    elif remaining_percent <= 2:
        status = "VERY CLOSE"
    elif remaining_percent <= 5:
        status = "NEAR TARGET"
    else:
        status = "HOLD"

    if stop is not None and current <= stop:
        status = "STOP LOSS HIT"

    return AlertResult(
        ticker=row["ticker"],
        bet_no=row["bet_no"],
        current_price=current,
        target_price=target,
        stop_loss=stop,
        buy_price=buy,
        qty=qty,
        profit=profit,
        profit_percent=profit_percent,
        remaining=remaining,
        remaining_percent=remaining_percent,
        status=status,
    )


def realized_pl(row):
    if row["sell_price"] is None:
        return None
    return (row["sell_price"] - row["buy_price"]) * row["qty"]


def db_row_to_report_position(row):
    return SimpleNamespace(
        bet_no=row["bet_no"],
        buy_date=row["buy_date"],
        sell_date=row["sell_date"],
        ticker=row["ticker"],
        qty=row["qty"],
        buy_price=row["buy_price"],
        trade_capital=(row["buy_price"] or 0) * (row["qty"] or 0),
        target_price=row["target_price"],
        stop_loss=row["stop_loss"],
        current_price=row["current_price"],
        sell_price=row["sell_price"],
        target_profit=None,
        target_loss=None,
        achieved_pl=realized_pl(row),
        status="Closed" if is_closed(row) else "Open",
    )


def generate_report_snapshots():
    rows = list_positions()
    positions = [db_row_to_report_position(row) for row in rows]
    generator = ReportGenerator()
    masked_path = generator.generate_html(positions, show_target_price=False)
    target_path = generator.generate_html(positions, show_target_price=True)
    logger.info(
        "Report snapshots generated: masked=%s targets=%s positions=%s",
        masked_path,
        target_path,
        len(positions),
    )
    return masked_path, target_path


def publish_masked_report(message):
    script_path = BASE_DIR / "publish_report.ps1"
    if not script_path.exists():
        logger.warning("Publish skipped: script not found at %s", script_path)
        return False

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Message",
        message,
    ]
    logger.info("Publishing masked report: command=%s", " ".join(command))
    result = subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )

    if result.stdout:
        logger.info("Publish stdout:\n%s", result.stdout.strip())
    if result.stderr:
        logger.warning("Publish stderr:\n%s", result.stderr.strip())

    if result.returncode != 0:
        logger.error("Publish failed: returncode=%s", result.returncode)
        return False

    logger.info("Publish completed successfully")
    return True


def format_whatsapp_message(result):
    if result.status == "TARGET HIT":
        title = "TARGET HIT"
        extra = f"Target    : ₹{result.target_price:.2f}"
    elif result.status == "STOP LOSS HIT":
        title = "STOP LOSS HIT"
        extra = f"Stop Loss : ₹{result.stop_loss:.2f}"
    elif result.status == "VERY CLOSE":
        title = "VERY CLOSE TO TARGET"
        extra = f"Target    : ₹{result.target_price:.2f}"
    elif result.status == "NEAR TARGET":
        title = "NEAR TARGET"
        extra = f"Target    : ₹{result.target_price:.2f}"
    else:
        title = result.status
        extra = f"Target    : ₹{result.target_price:.2f}"

    return (
        f"{title}\n"
        f"Bet       : {result.bet_no}\n"
        f"Stock     : {result.ticker}\n"
        f"Qty       : {result.qty}\n"
        f"Buy Price : ₹{result.buy_price:.2f}\n"
        f"Current   : ₹{result.current_price:.2f}\n"
        f"{extra}\n"
        f"Remaining : ₹{result.remaining:.2f}\n"
        f"Distance  : {result.remaining_percent:.2f}%\n"
        f"P/L       : ₹{result.profit:,.2f}\n"
        f"P/L %     : {result.profit_percent:.2f}%"
    )


def send_whatsapp(config, message):
    if not get_bool(config, "WHATSAPP", "ENABLE_WHATSAPP", False):
        logger.info("WhatsApp disabled. Message preview:\n%s", message)
        return

    token = config.get("WHATSAPP", "ACCESS_TOKEN", fallback="").strip()
    phone_number_id = config.get("WHATSAPP", "PHONE_NUMBER_ID", fallback="").strip()
    recipient = config.get("WHATSAPP", "RECIPIENT", fallback="").strip()

    if not token or not phone_number_id or not recipient:
        logger.warning("WhatsApp config missing. Message preview:\n%s", message)
        return

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    logger.info("WhatsApp API call started: recipient=%s", recipient)
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    logger.info("WhatsApp API response: status=%s body=%s", response.status_code, response.text)
    response.raise_for_status()


def send_eod_email(config, subject, body):
    if not get_bool(config, "EMAIL", "ENABLE_EMAIL", False):
        logger.info("Email disabled. Subject=%s", subject)
        return False

    sender = config.get("EMAIL", "EMAIL", fallback="").strip()
    password = config.get("EMAIL", "APP_PASSWORD", fallback="").strip()
    recipient = config.get("EMAIL", "TO_EMAIL", fallback="").strip()
    server = config.get("EMAIL", "SMTP_SERVER", fallback="smtp.gmail.com").strip()
    port = config.getint("EMAIL", "SMTP_PORT", fallback=587)

    if not sender or not password or not recipient:
        logger.warning("Email config missing. EOD email preview:\nSubject: %s\n%s", subject, body)
        return False

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    if isinstance(body, tuple):
        text_body, html_body = body
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(body)

    logger.info("EOD email send started: to=%s subject=%s", recipient, subject)
    with smtplib.SMTP(server, port) as smtp:
        smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(message)
    logger.info("EOD email sent: to=%s", recipient)
    return True


def run_market_check(config):
    logger.info("=" * 60)
    logger.info("Scheduled market check started")

    rows = open_positions()
    logger.info("Open bets: %s", len(rows))

    results = []
    for row in rows:
        ticker = row["ticker"]
        logger.info("Fetching price: bet_no=%s ticker=%s", row["bet_no"], ticker)
        try:
            price = latest_price(ticker)
            update_current_price(row["id"], price)
            logger.info("DB current price updated: bet_no=%s ticker=%s price=%s", row["bet_no"], ticker, price)
        except Exception:
            logger.exception("Live price unavailable: bet_no=%s ticker=%s", row["bet_no"], ticker)
            continue

        refreshed = dict(row)
        refreshed["current_price"] = price
        result = evaluate(refreshed)
        if result is None:
            logger.warning("Alert evaluation skipped: bet_no=%s ticker=%s", row["bet_no"], ticker)
            continue

        results.append(result)
        logger.info(
            "%-15s current=%10.2f target=%10.2f profit=%12.2f profit_pct=%8.2f remaining=%10.2f status=%s",
            result.ticker,
            result.current_price,
            result.target_price,
            result.profit,
            result.profit_percent,
            result.remaining,
            result.status,
        )

        if result.status in ("TARGET HIT", "STOP LOSS HIT", "VERY CLOSE", "NEAR TARGET"):
            message = format_whatsapp_message(result)
            logger.info("WHATSAPP ALERT\n%s", message)
            try:
                send_whatsapp(config, message)
            except Exception:
                logger.exception("WhatsApp send failed: bet_no=%s ticker=%s", result.bet_no, result.ticker)

    try:
        generate_report_snapshots()
    except Exception:
        logger.exception("Report snapshot generation failed")

    logger.info("Scheduled market check completed: evaluated=%s", len(results))
    return results


def run_eod_email_if_due(config, force=False):
    now = datetime.now()
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    market_end = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))

    if not force:
        if now.weekday() >= 5:
            logger.info("EOD email not due: weekend date=%s", today)
            return
        if now.time() <= market_end:
            logger.info(
                "EOD email not due: market still open now=%s market_end=%s",
                now.strftime("%H:%M:%S"),
                market_end.strftime("%H:%M:%S"),
            )
            return
        if now.time() < report_time:
            logger.info(
                "EOD email not due: waiting for report time now=%s report_time=%s",
                now.strftime("%H:%M:%S"),
                report_time.strftime("%H:%M:%S"),
            )
            return
        if state.get("eod_email_sent_date") == today:
            logger.info("EOD email already sent for %s", today)
            return
    elif state.get("eod_email_sent_date") == today:
        logger.info("EOD email force run requested even though %s is already marked sent", today)

    logger.info("EOD email due: force=%s date=%s", force, today)

    rows = list_positions()
    open_rows = [row for row in rows if not is_closed(row)]
    closed_rows = [row for row in rows if is_closed(row)]
    open_count = len(open_rows)
    closed_count = len(rows) - open_count
    realized_profit = sum(
        ((row["sell_price"] or 0) - row["buy_price"]) * row["qty"]
        for row in rows
        if is_closed(row) and row["sell_price"] is not None
    )
    open_trades_capital = sum((row["buy_price"] or 0) * (row["qty"] or 0) for row in open_rows)
    realized_return = (realized_profit / open_trades_capital * 100) if open_trades_capital else 0

    def parse_date(value):
        return parse_shared_date(value)

    def display_date(value):
        return display_shared_date(value)

    def safe(value):
        return escape(str(value or "-"))

    today_date = now.date()

    normalized_buy_dates = normalized_buy_date_map(rows, today=today_date)

    def buy_date_for(row):
        return normalized_buy_dates.get(position_identity(row)) or parse_date(row["buy_date"])

    def display_buy_date(row):
        parsed = buy_date_for(row)
        if parsed:
            return parsed.strftime("%d/%m/%Y")
        return row["buy_date"] or "-"

    def open_profit(row):
        if row["current_price"] is None:
            return None
        return (row["current_price"] - row["buy_price"]) * row["qty"]

    def open_profit_pct(row):
        if row["current_price"] is None or not row["buy_price"]:
            return None
        return ((row["current_price"] - row["buy_price"]) / row["buy_price"]) * 100

    def money(value):
        if value is None:
            return "-"
        return f"₹{value:,.2f}"

    def pct(value):
        if value is None:
            return "-"
        return f"{value:+.2f}%"

    target_hits = [
        row for row in open_rows
        if row["current_price"] is not None
        and row["target_price"] is not None
        and row["current_price"] >= row["target_price"]
    ]
    near_targets = []
    for row in open_rows:
        if row["current_price"] is None or row["target_price"] in (None, 0):
            continue
        remaining = row["target_price"] - row["current_price"]
        remaining_pct = remaining / row["target_price"] * 100
        if 0 <= remaining_pct <= 5:
            near_targets.append((row, remaining, remaining_pct))

    priced_open_rows = [row for row in open_rows if open_profit_pct(row) is not None]
    top_winners = sorted(priced_open_rows, key=open_profit_pct, reverse=True)[:5]
    top_losers = sorted(priced_open_rows, key=open_profit_pct)[:5]
    recent_closed = sorted(closed_rows, key=lambda row: row["sell_date"] or row["buy_date"] or "", reverse=True)[:5]
    new_bets_today = [
        row for row in rows
        if parse_date(row["created_at"]) == today_date or buy_date_for(row) == today_date
    ]
    closed_today = [row for row in closed_rows if parse_date(row["sell_date"]) == today_date]
    activity_count = len(new_bets_today) + len(closed_today) + len(target_hits)

    def realized_pl(row):
        if row["sell_price"] is None:
            return None
        return ((row["sell_price"] or 0) - row["buy_price"]) * row["qty"]

    def holding_days(row):
        buy_date = buy_date_for(row)
        sell_date = parse_date(row["sell_date"])
        if not buy_date or not sell_date:
            return "-"
        days = max((sell_date - buy_date).days, 0)
        return f"{days} day" if days == 1 else f"{days} days"

    def rows_html(items, columns, empty_text):
        if not items:
            return f"<tr><td colspan='{len(columns)}' class='empty'>{empty_text}</td></tr>"
        return "\n".join(items)

    new_bet_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']} | Added {display_date(row['created_at'])}</span></td>
                <td>{display_buy_date(row)}</td>
                <td>{money(row['buy_price'])}</td>
                <td>{money(row['target_price'])}</td>
                <td>{money((row['buy_price'] or 0) * (row['qty'] or 0))}</td>
            </tr>
            """
            for row in sorted(new_bets_today, key=lambda item: item["created_at"] or item["buy_date"] or "", reverse=True)
        ],
        ["Ticker", "Buy Date", "Buy Price", "Target", "Investment"],
        "No new bets added today.",
    )
    closed_today_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']} | Held {holding_days(row)}</span></td>
                <td>{display_date(row['sell_date'])}</td>
                <td>{money(row['sell_price'])}</td>
                <td class="{ 'good' if (realized_pl(row) or 0) >= 0 else 'bad' }">{money(realized_pl(row))}</td>
            </tr>
            """
            for row in sorted(closed_today, key=lambda item: item["sell_date"] or "", reverse=True)
        ],
        ["Ticker", "Sell Date", "Sell Price", "Realized P/L"],
        "No bets were closed today.",
    )
    target_hit_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']}</span></td>
                <td>{money(row['current_price'])}</td>
                <td>{money(row['target_price'])}</td>
                <td class="good">{pct(open_profit_pct(row))}</td>
            </tr>
            """
            for row in target_hits
        ],
        ["Ticker", "Current", "Target", "P/L %"],
        "No target hits today.",
    )
    near_target_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']}</span></td>
                <td>{money(row['current_price'])}</td>
                <td>{money(row['target_price'])}</td>
                <td>{remaining_pct:.2f}%</td>
            </tr>
            """
            for row, _remaining, remaining_pct in near_targets[:5]
        ],
        ["Ticker", "Current", "Target", "Distance"],
        "No bets within 5% of target.",
    )
    winner_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']}</span></td>
                <td>{money(open_profit(row))}</td>
                <td class="good">{pct(open_profit_pct(row))}</td>
            </tr>
            """
            for row in top_winners
        ],
        ["Ticker", "P/L", "P/L %"],
        "No open winners yet.",
    )
    loser_rows = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']}</span></td>
                <td>{money(open_profit(row))}</td>
                <td class="bad">{pct(open_profit_pct(row))}</td>
            </tr>
            """
            for row in top_losers
        ],
        ["Ticker", "P/L", "P/L %"],
        "No open losers yet.",
    )
    closed_rows_html = rows_html(
        [
            f"""
            <tr>
                <td><strong>{safe(row['ticker'])}</strong><span>Bet {row['bet_no']}</span></td>
                <td>{display_date(row['sell_date'])}</td>
                <td class="{ 'good' if (realized_pl(row) or 0) >= 0 else 'bad' }">{money(realized_pl(row))}</td>
            </tr>
            """
            for row in recent_closed
        ],
        ["Ticker", "Sell Date", "Realized P/L"],
        "No recent closed bets.",
    )

    text_body = (
        "End of day portfolio summary\n\n"
        f"Date: {today}\n"
        f"Total bets: {len(rows)}\n"
        f"Open bets: {open_count}\n"
        f"Closed bets: {closed_count}\n"
        f"Open trades capital: {money(open_trades_capital)}\n"
        f"Realized profit: {money(realized_profit)}\n"
        f"Realized return: {realized_return:.2f}%\n"
        f"Today's activity: {activity_count}\n"
        f"New bets today: {len(new_bets_today)}\n"
        f"Closed today: {len(closed_today)}\n"
        f"Target hits: {len(target_hits)}\n"
        f"Near targets: {len(near_targets)}\n\n"
        "Reports:\n"
        "Masked: https://anandukanagaraj.github.io/myportfoliobets-report/\n"
        "With targets: http://127.0.0.1:5002/report-targets\n"
    )
    html_body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ margin:0; background:#eef3f8; color:#172033; font-family:Arial, Helvetica, sans-serif; }}
    .wrap {{ max-width:920px; margin:0 auto; padding:24px; }}
    .hero {{ background:linear-gradient(135deg,#1e3a8a,#0f766e); color:#fff; border-radius:8px; padding:24px; }}
    .hero p {{ margin:8px 0 0; color:#dbeafe; }}
    h1 {{ margin:0; font-size:28px; }}
    h2 {{ margin:0; font-size:17px; }}
    .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }}
    .card {{ background:#fff; border:1px solid #dce5ef; border-radius:8px; padding:14px; }}
    .card span {{ display:block; color:#64748b; font-size:12px; font-weight:bold; text-transform:uppercase; }}
    .card strong {{ display:block; margin-top:8px; font-size:21px; white-space:nowrap; }}
    .activity {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:16px 0; }}
    .activity-card {{ background:#fff; border:1px solid #dce5ef; border-radius:8px; padding:16px; }}
    .activity-card span {{ display:block; color:#64748b; font-size:12px; font-weight:bold; text-transform:uppercase; }}
    .activity-card strong {{ display:block; margin-top:6px; color:#0f172a; font-size:28px; }}
    .activity-card p {{ margin:6px 0 0; color:#64748b; font-size:13px; }}
    .section {{ background:#fff; border:1px solid #dce5ef; border-radius:8px; margin-top:16px; overflow:hidden; }}
    .section h2 {{ padding:16px; border-bottom:1px solid #dce5ef; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ padding:11px 13px; border-bottom:1px solid #edf2f7; text-align:right; white-space:nowrap; }}
    th:first-child, td:first-child {{ text-align:left; }}
    th {{ background:#f8fafc; color:#475569; font-size:12px; text-transform:uppercase; }}
    td span {{ display:block; margin-top:3px; color:#64748b; font-size:12px; }}
    .good {{ color:#087f5b; font-weight:bold; }}
    .bad {{ color:#c92a2a; font-weight:bold; }}
    .empty {{ color:#64748b; text-align:center; }}
    .links {{ margin-top:16px; }}
    .links a {{ display:inline-block; margin-right:10px; padding:10px 13px; border-radius:6px; background:#0369a1; color:#fff; text-decoration:none; }}
    @media (max-width:700px) {{ .cards, .activity {{ grid-template-columns:repeat(2,1fr); }} .wrap {{ padding:12px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>My Portfolio Bets EOD</h1>
      <p>{today} | {activity_count} activities today | {open_count} open | {closed_count} closed</p>
    </div>

    <div class="cards">
      <div class="card"><span>Open Trades Capital</span><strong>{money(open_trades_capital)}</strong></div>
      <div class="card"><span>Realized Profit</span><strong class="{'good' if realized_profit >= 0 else 'bad'}">{money(realized_profit)}</strong></div>
      <div class="card"><span>Realized Return</span><strong class="{'good' if realized_return >= 0 else 'bad'}">{realized_return:.2f}%</strong></div>
      <div class="card"><span>Total Bets</span><strong>{len(rows)}</strong></div>
    </div>

    <div class="activity">
      <div class="activity-card"><span>New Bets Today</span><strong>{len(new_bets_today)}</strong><p>Fresh bets added to the portfolio.</p></div>
      <div class="activity-card"><span>Closed Today</span><strong>{len(closed_today)}</strong><p>Bets realized with sell date as today.</p></div>
      <div class="activity-card"><span>Target Hits Now</span><strong>{len(target_hits)}</strong><p>Open bets currently at or above target.</p></div>
    </div>

    <div class="section">
      <h2>Today's New Bets</h2>
      <table><thead><tr><th>Ticker</th><th>Buy Date</th><th>Buy Price</th><th>Target</th><th>Investment</th></tr></thead><tbody>{new_bet_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Today's Closed Bets</h2>
      <table><thead><tr><th>Ticker</th><th>Sell Date</th><th>Sell Price</th><th>Realized P/L</th></tr></thead><tbody>{closed_today_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Target Hits</h2>
      <table><thead><tr><th>Ticker</th><th>Current</th><th>Target</th><th>P/L %</th></tr></thead><tbody>{target_hit_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Near Target</h2>
      <table><thead><tr><th>Ticker</th><th>Current</th><th>Target</th><th>Distance</th></tr></thead><tbody>{near_target_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Top Open Winners</h2>
      <table><thead><tr><th>Ticker</th><th>P/L</th><th>P/L %</th></tr></thead><tbody>{winner_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Top Open Losers</h2>
      <table><thead><tr><th>Ticker</th><th>P/L</th><th>P/L %</th></tr></thead><tbody>{loser_rows}</tbody></table>
    </div>

    <div class="section">
      <h2>Recent Closed Bets</h2>
      <table><thead><tr><th>Ticker</th><th>Sell Date</th><th>Realized P/L</th></tr></thead><tbody>{closed_rows_html}</tbody></table>
    </div>

    <div class="links">
      <a href="https://anandukanagaraj.github.io/myportfoliobets-report/">Masked Report</a>
      <a href="http://127.0.0.1:5002/report-targets">Report With Targets</a>
    </div>
  </div>
</body>
</html>"""

    sent = send_eod_email(config, f"My Portfolio Bets EOD - {today}", (text_body, html_body))
    if sent:
        state["eod_email_sent_date"] = today
        save_state(state)
        if config.getboolean("PUBLISH", "ENABLE_AFTER_EOD", fallback=True):
            try:
                generate_report_snapshots()
                published = publish_masked_report(f"EOD report {today}")
                if not published:
                    logger.error("EOD publish did not complete successfully")
            except Exception:
                logger.exception("EOD publish failed")


def is_market_time(config):
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    start = parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15"))
    end = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))
    return start <= now.time() <= end


def next_weekday_at(target_time, now=None):
    now = now or datetime.now()
    candidate = datetime.combine(now.date(), target_time)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def next_scheduler_wakeup(config, next_market_check_at):
    now = datetime.now()
    start_time = parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15"))
    end_time = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    start_at = datetime.combine(now.date(), start_time)
    end_at = datetime.combine(now.date(), end_time)
    report_at = datetime.combine(now.date(), report_time)

    candidates = []
    if now.weekday() < 5:
        if now < start_at:
            candidates.append(start_at)
        elif start_at <= now <= end_at and next_market_check_at:
            candidates.append(next_market_check_at)

        if now < report_at:
            candidates.append(report_at)

    candidates.append(next_weekday_at(start_time, now))

    next_event = min(candidate for candidate in candidates if candidate > now)
    seconds = max((next_event - now).total_seconds(), 1)
    # Wake occasionally even when the next event is far away, so config changes are picked up.
    return min(seconds, 300)


def run_once(force=False):
    config = load_config()
    logger.info("Scheduler run_once force=%s", force)
    if force or is_market_time(config):
        run_market_check(config)
    else:
        logger.info("Outside Indian market hours. Skipping market check.")
    run_eod_email_if_due(config, force=force)


def run_forever():
    config = load_config()
    interval = config.getint("MARKET", "CHECK_INTERVAL_MINUTES", fallback=30)
    next_market_check_at = None
    logger.info("Scheduler started: interval_minutes=%s", interval)
    logger.info(
        "Market hours: %s-%s report_time=%s",
        config.get("MARKET", "START_TIME", fallback="09:15"),
        config.get("MARKET", "END_TIME", fallback="15:30"),
        config.get("MARKET", "REPORT_TIME", fallback="15:35"),
    )

    while True:
        try:
            config = load_config()
            interval = config.getint("MARKET", "CHECK_INTERVAL_MINUTES", fallback=30)
            now = datetime.now()
            start = today_at(parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15")))
            end = today_at(parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30")))

            if next_market_check_at is None or next_market_check_at.date() != now.date():
                next_market_check_at = start if now < start else now

            if now.weekday() < 5 and start <= now <= end and now >= next_market_check_at:
                logger.info("Market cycle due: scheduled_for=%s", next_market_check_at.strftime("%Y-%m-%d %H:%M:%S"))
                run_market_check(config)
                next_market_check_at = datetime.now() + timedelta(minutes=interval)
                logger.info("Next market cycle scheduled_for=%s", next_market_check_at.strftime("%Y-%m-%d %H:%M:%S"))

            run_eod_email_if_due(config, force=False)
            sleep_seconds = next_scheduler_wakeup(config, next_market_check_at)
            logger.info("Scheduler sleeping for %.0f seconds", sleep_seconds)
        except Exception:
            logger.exception("Scheduler cycle failed")
            sleep_seconds = 60
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run V3 scheduled price checks and EOD email.")
    parser.add_argument("--once", action="store_true", help="Run one scheduler cycle and exit.")
    parser.add_argument("--force", action="store_true", help="Run checks even outside market hours.")
    args = parser.parse_args()

    instance_lock = SchedulerInstanceLock(LOCK_PATH)
    if not instance_lock.acquire():
        raise SystemExit("Scheduler is already running. Stop the existing process before starting another one.")

    try:
        if args.once:
            run_once(force=args.force)
        else:
            run_forever()
    finally:
        instance_lock.release()
