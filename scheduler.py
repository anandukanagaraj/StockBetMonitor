import configparser
import json
import logging
import os
import shutil
import smtplib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from types import SimpleNamespace

import requests

if os.name == "nt":
    import msvcrt

    fcntl = None
else:
    import fcntl

    msvcrt = None

from db import (
    DEFAULT_STOCK_GROUP_CODE,
    is_closed,
    list_positions,
    list_stock_price_alerts,
    mark_stock_price_alert_checked,
    mark_stock_price_alert_triggered,
    record_stock_booking_simulation_snapshot,
    record_portfolio_performance_snapshot,
    update_current_price,
    get_user_by_username,
)
from options_db import list_option_strategies
from services.db_backup import backup_database
from services.date_utils import (
    display_date as display_shared_date,
    normalized_buy_date_map,
    parse_date as parse_shared_date,
    position_identity,
)
from services.number_format import format_indian_inr, format_indian_money, format_indian_money_signed
from services.option_charges import option_strategy_risk_capital
from services.portfolio_analytics import build_portfolio_analytics
from services.report_cleanup import cleanup_old_reports, update_report_manifest
from services.scheduler_health import read_heartbeat, write_heartbeat
from services.v1_report_generator import ReportGenerator
from services.stock_profit_booking_simulator import simulate_stock_profit_booking
from services.yahoo_service import latest_price, latest_prices
from user_context import set_current_user_id


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = BASE_DIR / "data" / "scheduler_state.json"
LOCK_PATH = BASE_DIR / "data" / "scheduler.lock"
REPORTS_DIR = BASE_DIR / "reports"
_last_stale_alert_date = None


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
    position_id: int | None = None


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
            if msvcrt:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            if msvcrt:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
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
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp_path, STATE_PATH)


# Alert levels: an alert is sent when a bet moves UP a level (or newly hits its
# stop loss), and once per day at the first check while it stays at a level.
# Dropping back down a level is recorded silently, so prices hovering around a
# threshold don't send a stream of alerts.
ALERT_LEVELS = {"HOLD": 0, "NEAR TARGET": 1, "VERY CLOSE": 2, "TARGET HIT": 3, "STOP LOSS HIT": 3}


def alert_key(result):
    return str(result.position_id) if result.position_id is not None else f"{result.ticker}|{result.bet_no}"


def should_send_alert(previous, status, today):
    if ALERT_LEVELS.get(status, 0) == 0:
        return False
    if not previous:
        return True
    if previous.get("sent_date") != today:
        return True
    previous_status = previous.get("status")
    if status == "STOP LOSS HIT":
        return previous_status != "STOP LOSS HIT"
    return ALERT_LEVELS.get(status, 0) > ALERT_LEVELS.get(previous_status, 0)


def next_alert_state(previous, status, today, sent):
    return {
        "status": status,
        "sent_date": today if sent else (previous or {}).get("sent_date"),
    }


def scheduler_stock_group_codes(config):
    raw_value = config.get("SCHEDULER", "STOCK_GROUP_CODES", fallback=DEFAULT_STOCK_GROUP_CODE)
    codes = [
        item.strip().upper()
        for item in raw_value.replace(";", ",").split(",")
        if item.strip()
    ]
    return codes or [DEFAULT_STOCK_GROUP_CODE]


def scheduler_positions(config):
    rows = []
    seen_ids = set()
    for code in scheduler_stock_group_codes(config):
        group_rows = list_positions(trade_group_code=code, reportable_only=True)
        logger.debug("Scheduler stock group loaded: code=%s rows=%s", code, len(group_rows))
        for row in group_rows:
            if row["id"] in seen_ids:
                continue
            rows.append(row)
            seen_ids.add(row["id"])
    logger.info(
        "Scheduler stock groups enabled: codes=%s positions=%s",
        ", ".join(scheduler_stock_group_codes(config)),
        len(rows),
    )
    return rows


def open_positions(config):
    return [row for row in scheduler_positions(config) if not is_closed(row)]


def price_fetch_workers(config, ticker_count):
    configured = config.getint("SCHEDULER", "PRICE_FETCH_WORKERS", fallback=8)
    return max(1, min(configured, 16, ticker_count or 1))


def fetch_latest_prices(tickers, config, context="scheduled"):
    unique_tickers = {}
    for ticker in tickers:
        normalized = (ticker or "").strip().upper()
        if normalized and normalized not in unique_tickers:
            unique_tickers[normalized] = ticker

    if not unique_tickers:
        return {}, {}

    prices = latest_prices(unique_tickers.values())
    unresolved = {
        normalized: ticker
        for normalized, ticker in unique_tickers.items()
        if normalized not in prices
    }
    workers = price_fetch_workers(config, len(unresolved))
    logger.info(
        "%s quote fetch: unique_tickers=%s bulk_resolved=%s fallback_workers=%s",
        context,
        len(unique_tickers),
        len(prices),
        workers,
    )
    failures = {}
    if not unresolved:
        return prices, failures
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quote") as executor:
        futures = {
            executor.submit(latest_price, ticker): normalized
            for normalized, ticker in unresolved.items()
        }
        for future in as_completed(futures):
            normalized = futures[future]
            try:
                prices[normalized] = future.result()
            except Exception as exc:
                failures[normalized] = exc
                logger.error(
                    "%s quote fetch failed: ticker=%s error=%s",
                    context,
                    unresolved[normalized],
                    exc,
                )

    logger.info(
        "%s parallel quote fetch completed: resolved=%s failed=%s",
        context,
        len(prices),
        len(failures),
    )
    return prices, failures


def refresh_open_position_prices(config, context="scheduled"):
    rows = open_positions(config)
    logger.info("%s price refresh started: open_bets=%s", context, len(rows))

    refreshed_rows = []
    failed_rows = []
    prices, _failures = fetch_latest_prices(
        (row["ticker"] for row in rows),
        config,
        context=f"{context} price refresh",
    )
    for row in rows:
        ticker = row["ticker"]
        normalized = (ticker or "").strip().upper()
        if normalized not in prices:
            failed_rows.append(row)
            continue
        try:
            price = prices[normalized]
            update_current_price(row["id"], price)
            refreshed = dict(row)
            refreshed["current_price"] = price
            refreshed_rows.append(refreshed)
            logger.info(
                "%s price refresh updated: bet_no=%s ticker=%s price=%s",
                context,
                row["bet_no"],
                ticker,
                price,
            )
        except Exception:
            failed_rows.append(row)
            logger.exception("%s price refresh failed: bet_no=%s ticker=%s", context, row["bet_no"], ticker)

    logger.info(
        "%s price refresh completed: updated=%s failed=%s",
        context,
        len(refreshed_rows),
        len(failed_rows),
    )
    return refreshed_rows, failed_rows


def record_portfolio_snapshot():
    positions = list_positions(trade_group_code=None, reportable_only=True)
    strategies = list_option_strategies(reportable_only=True)
    analytics = build_portfolio_analytics(positions, strategies, option_strategy_risk_capital)
    record_portfolio_performance_snapshot(analytics["summary"])
    logger.info(
        "Portfolio performance snapshot recorded: total_pl=%s realized_pl=%s unrealized_pl=%s",
        analytics["summary"]["total_actual_pl"],
        analytics["summary"]["total_realized_pl"],
        analytics["summary"]["total_unrealized_pl"],
    )


def evaluate(row, stop_loss_enabled=True):
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
    elif current >= buy and remaining_percent <= 2:
        status = "VERY CLOSE"
    elif current >= buy and remaining_percent <= 5:
        status = "NEAR TARGET"
    else:
        status = "HOLD"

    if stop_loss_enabled and stop is not None and current <= stop:
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
        position_id=row["id"] if "id" in row.keys() else None,
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


def generate_report_snapshots(config):
    rows = scheduler_positions(config)
    positions = [db_row_to_report_position(row) for row in rows]
    generator = ReportGenerator()
    masked_path = generator.generate_html(positions, show_target_price=False)
    target_path = generator.generate_html(positions, show_target_price=True)
    app_target_path = generator.generate_html(positions, show_target_price=True, include_app_nav=True)
    update_report_manifest(
        {
            "masked": masked_path,
            "targets": target_path,
            "app_targets": app_target_path,
        }
    )
    keep_days = config.getint("APP", "REPORT_KEEP_DAYS", fallback=14)
    cleanup_old_reports(REPORTS_DIR, keep_days=keep_days)
    logger.info(
        "Report snapshots generated: masked=%s targets=%s app_targets=%s positions=%s",
        masked_path,
        target_path,
        app_target_path,
        len(positions),
    )
    return masked_path, target_path


def record_stock_booking_snapshot(rows, snapshot_date):
    simulation = simulate_stock_profit_booking(rows, today=snapshot_date)
    snapshot_id = record_stock_booking_simulation_snapshot(
        simulation["summary"],
        snapshot_date=snapshot_date.isoformat(),
    )
    analytics = simulation["summary"].get("analytics") or {}
    logger.info(
        "Stock booking simulation snapshot recorded: id=%s date=%s bets=%s recommended=%s highest_profit=%s",
        snapshot_id,
        snapshot_date.isoformat(),
        simulation["summary"].get("bets"),
        analytics.get("best_label"),
        analytics.get("highest_profit_label"),
    )
    return snapshot_id


def publish_command(message):
    if os.name == "nt":
        script_path = BASE_DIR / "publish_report.ps1"
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), "-Message", message]
    else:
        script_path = BASE_DIR / "publish_report.sh"
        command = [shutil.which("bash") or "/bin/bash", str(script_path), "--message", message]
    return script_path, command


def warn_if_not_ist():
    offset = datetime.now().astimezone().utcoffset()
    if offset != timedelta(hours=5, minutes=30):
        logger.warning(
            "Local clock is UTC%s, not IST (UTC+05:30). Market hours in config.ini are IST; "
            "set TZ=Asia/Kolkata for the scheduler process.",
            datetime.now().astimezone().strftime("%z"),
        )


def publish_masked_report(message):
    script_path, command = publish_command(message)
    if not script_path.exists():
        logger.warning("Publish skipped: script not found at %s", script_path)
        return False

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
        title = "====================\n   TARGET HIT\n===================="
        trigger_line = (
            f"Target: {format_indian_money(result.target_price)}\n"
            "ACTION: Review and book profit"
        )
    elif result.status == "STOP LOSS HIT":
        title = "STOP LOSS HIT"
        trigger_line = f"Stop loss: {format_indian_money(result.stop_loss)}"
    elif result.status == "VERY CLOSE":
        title = "VERY CLOSE TO TARGET"
        trigger_line = (
            f"Target: {format_indian_money(result.target_price)} · "
            f"Gap: {format_indian_money(result.remaining)} ({result.remaining_percent:.2f}%)"
        )
    elif result.status == "NEAR TARGET":
        title = "NEAR TARGET"
        trigger_line = (
            f"Target: {format_indian_money(result.target_price)} · "
            f"Gap: {format_indian_money(result.remaining)} ({result.remaining_percent:.2f}%)"
        )
    else:
        title = result.status
        trigger_line = f"Target: {format_indian_money(result.target_price)}"

    return (
        f"{title}\n"
        f"{result.ticker} · Bet #{result.bet_no}\n\n"
        f"Current: {format_indian_money(result.current_price)}\n"
        f"{trigger_line}\n"
        f"Position: {result.qty:g} @ {format_indian_money(result.buy_price)}\n\n"
        f"P/L: {format_indian_money_signed(result.profit)} ({result.profit_percent:+.2f}%)"
    )


def format_telegram_message(result):
    status_styles = {
        "TARGET HIT": ("🟢", "TARGET HIT", "Review and book profit"),
        "VERY CLOSE": ("🟡", "VERY CLOSE TO TARGET", "Watch closely"),
        "NEAR TARGET": ("🟡", "NEAR TARGET", "Target is close"),
        "STOP LOSS HIT": ("🔴", "STOP LOSS HIT", "Review risk"),
    }
    icon, title, action = status_styles.get(result.status, ("🔵", result.status, "Monitor"))

    if result.status == "STOP LOSS HIT":
        trigger_line = (
            f"Stop loss: {format_indian_money(result.stop_loss)}"
            if result.stop_loss is not None
            else "Stop loss reached"
        )
    elif result.status in {"VERY CLOSE", "NEAR TARGET"}:
        trigger_line = (
            f"Target: {format_indian_money(result.target_price)} · "
            f"Gap: {format_indian_money(result.remaining)} ({result.remaining_percent:.2f}%)"
        )
    else:
        trigger_line = f"Target: {format_indian_money(result.target_price)}"

    profit_icon = "🟢" if result.profit >= 0 else "🔴"
    return (
        f"{icon} <b>{escape(title)}</b>\n"
        f"<b>{escape(result.ticker)}</b> · Bet #{result.bet_no}\n\n"
        f"Current: <b>{format_indian_money(result.current_price)}</b>\n"
        f"{escape(trigger_line)}\n"
        f"Position: {result.qty:g} @ {format_indian_money(result.buy_price)}\n\n"
        f"{profit_icon} P/L: <b>{format_indian_money_signed(result.profit)}</b> ({result.profit_percent:+.2f}%)\n"
        f"Action: <b>{escape(action)}</b>"
    )


def send_whatsapp(config, message):
    if not get_bool(config, "WHATSAPP", "ENABLE_WHATSAPP", False):
        logger.info("WhatsApp disabled. Message preview:\n%s", message)
        return False

    token = config.get("WHATSAPP", "ACCESS_TOKEN", fallback="").strip()
    phone_number_id = config.get("WHATSAPP", "PHONE_NUMBER_ID", fallback="").strip()
    recipient = config.get("WHATSAPP", "RECIPIENT", fallback="").strip()

    if not token or not phone_number_id or not recipient:
        logger.warning("WhatsApp config missing. Message preview:\n%s", message)
        return False

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
    return True


def send_telegram(config, message):
    if not get_bool(config, "TELEGRAM", "ENABLE_TELEGRAM", False):
        logger.info("Telegram disabled. Message preview:\n%s", message)
        return False

    token = config.get("TELEGRAM", "BOT_TOKEN", fallback="").strip()
    chat_id = config.get("TELEGRAM", "CHAT_ID", fallback="").strip()
    if not token or not chat_id:
        logger.warning("Telegram config missing. Message preview:\n%s", message)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    for attempt in range(1, 4):
        try:
            logger.info("Telegram API call started: chat_id=%s attempt=%s/3", chat_id, attempt)
            response = requests.post(url, json=payload, timeout=30)
            logger.info("Telegram API response: status=%s body=%s", response.status_code, response.text)
            response.raise_for_status()
            return True
        except requests.RequestException:
            if attempt == 3:
                logger.error("Telegram API failed after %s attempts; scheduler will continue", attempt, exc_info=True)
                return False
            delay = 2 ** attempt
            logger.warning("Telegram API attempt %s failed; retrying in %s seconds", attempt, delay, exc_info=True)
            time.sleep(delay)
    return False


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


def send_alert(config, result, message):
    try:
        if send_whatsapp(config, message):
            return {"whatsapp": True, "telegram": False, "email": False}
    except Exception:
        logger.exception("WhatsApp send failed; using Telegram and email fallback: bet_no=%s ticker=%s", result.bet_no, result.ticker)

    delivered = {"whatsapp": False, "telegram": False, "email": False}
    try:
        telegram_message = format_telegram_message(result)
        logger.info("TELEGRAM ALERT\n%s", telegram_message)
        delivered["telegram"] = send_telegram(config, telegram_message)
    except Exception:
        logger.exception("Telegram fallback failed: bet_no=%s ticker=%s", result.bet_no, result.ticker)

    if delivered["telegram"]:
        logger.info("Alert delivered by Telegram; email fallback not needed: bet_no=%s ticker=%s", result.bet_no, result.ticker)
        return delivered

    try:
        delivered["email"] = send_eod_email(
            config,
            f"Portfolio alert: {result.status} - {result.ticker} (Bet {result.bet_no})",
            message,
        )
    except Exception:
        logger.exception("Email fallback failed: bet_no=%s ticker=%s", result.bet_no, result.ticker)

    logger.info("Alert fallback completed: bet_no=%s ticker=%s telegram=%s email=%s", result.bet_no, result.ticker, delivered["telegram"], delivered["email"])
    return delivered


def stock_price_alert_triggered(alert, price):
    direction = (alert.get("direction") or "ABOVE").upper()
    trigger_price = alert["trigger_price"]
    if direction == "BELOW":
        return price <= trigger_price
    return price >= trigger_price


def format_stock_price_alert_message(alert, price):
    direction = (alert.get("direction") or "ABOVE").upper()
    direction_text = "at or below" if direction == "BELOW" else "at or above"
    note = (alert.get("notes") or "").strip()
    body = note if note else "Price alert condition reached."
    return (
        "STOCK PRICE ALERT\n"
        f"{alert['ticker']}\n\n"
        f"{body}\n\n"
        f"Current: {format_indian_inr(price)}\n"
        f"Alert: {direction_text} {format_indian_inr(alert['trigger_price'])}"
    )


def format_stock_price_alert_telegram_message(alert, price):
    direction = (alert.get("direction") or "ABOVE").upper()
    direction_text = "at or below" if direction == "BELOW" else "at or above"
    note = escape((alert.get("notes") or "").strip() or "Price alert condition reached.")
    return (
        "🔔 <b>STOCK PRICE ALERT</b>\n"
        f"<b>{escape(alert['ticker'])}</b>\n\n"
        f"{note}\n\n"
        f"Current: <b>{format_indian_inr(price)}</b>\n"
        f"Alert: {escape(direction_text)} {format_indian_inr(alert['trigger_price'])}"
    )


def send_stock_price_alert(config, alert, price):
    channel = (alert.get("channel") or "BOTH").upper()
    whatsapp_message = format_stock_price_alert_message(alert, price)
    telegram_message = format_stock_price_alert_telegram_message(alert, price)
    delivered = {"whatsapp": False, "telegram": False}

    if channel in {"BOTH", "WHATSAPP"}:
        try:
            logger.info("STOCK PRICE ALERT WHATSAPP\n%s", whatsapp_message)
            delivered["whatsapp"] = send_whatsapp(config, whatsapp_message)
        except Exception:
            logger.exception("Stock price alert WhatsApp failed: id=%s ticker=%s", alert["id"], alert["ticker"])

    if channel == "TELEGRAM" or (channel == "BOTH" and not delivered["whatsapp"]):
        try:
            logger.info("STOCK PRICE ALERT TELEGRAM\n%s", telegram_message)
            delivered["telegram"] = send_telegram(config, telegram_message)
        except Exception:
            logger.exception("Stock price alert Telegram failed: id=%s ticker=%s", alert["id"], alert["ticker"])

    logger.info(
        "Stock price alert delivery completed: id=%s ticker=%s channel=%s whatsapp=%s telegram=%s",
        alert["id"],
        alert["ticker"],
        channel,
        delivered["whatsapp"],
        delivered["telegram"],
    )
    return delivered


def run_stock_price_alert_check(config, known_prices=None):
    alerts = [
        alert
        for alert in list_stock_price_alerts(active_only=True)
        if alert.get("triggered_at") is None
    ]
    logger.info("Stock price alert check started: active_pending=%s", len(alerts))
    triggered = 0
    failed = 0
    prices = dict(known_prices or {})
    missing_tickers = [
        alert["ticker"]
        for alert in alerts
        if (alert["ticker"] or "").strip().upper() not in prices
    ]
    fetched_prices, _failures = fetch_latest_prices(
        missing_tickers,
        config,
        context="Stock price alerts",
    )
    prices.update(fetched_prices)
    for alert in alerts:
        try:
            normalized = (alert["ticker"] or "").strip().upper()
            if normalized not in prices:
                failed += 1
                continue
            price = prices[normalized]
            mark_stock_price_alert_checked(alert["id"], price)
            logger.info(
                "Stock price alert checked: id=%s ticker=%s current=%s direction=%s trigger=%s",
                alert["id"],
                alert["ticker"],
                price,
                alert["direction"],
                alert["trigger_price"],
            )
            if not stock_price_alert_triggered(alert, price):
                continue
            delivered = send_stock_price_alert(config, alert, price)
            if delivered["whatsapp"] or delivered["telegram"]:
                mark_stock_price_alert_triggered(alert["id"], price)
                triggered += 1
            else:
                logger.warning(
                    "Stock price alert condition met but no channel delivered; keeping pending: id=%s ticker=%s",
                    alert["id"],
                    alert["ticker"],
                )
        except Exception:
            failed += 1
            logger.exception("Stock price alert check failed: id=%s ticker=%s", alert["id"], alert["ticker"])

    logger.info("Stock price alert check completed: triggered=%s failed=%s", triggered, failed)
    return triggered


def maybe_alert_stale_scheduler(config):
    global _last_stale_alert_date
    if not is_market_time(config):
        return

    heartbeat = read_heartbeat()
    if not heartbeat or not heartbeat.get("last_market_check_at"):
        return

    interval = config.getint("MARKET", "CHECK_INTERVAL_MINUTES", fallback=30)
    stale_minutes = config.getint("APP", "SCHEDULER_STALE_MINUTES", fallback=45)
    threshold = max(stale_minutes, interval * 2 + 5)
    try:
        last_check = datetime.fromisoformat(heartbeat["last_market_check_at"])
    except ValueError:
        return

    age_minutes = (datetime.now() - last_check).total_seconds() / 60
    if age_minutes <= threshold:
        return

    today = datetime.now().date().isoformat()
    if _last_stale_alert_date == today:
        return

    message = (
        "StockMonitor scheduler heartbeat is stale.\n"
        f"Last market check: {heartbeat['last_market_check_at']}\n"
        f"Age: {age_minutes:.0f} minutes"
    )
    logger.warning("Scheduler stale alert triggered: age_minutes=%.1f threshold=%s", age_minutes, threshold)
    if send_telegram(config, message):
        _last_stale_alert_date = today


def failed_price_alert_message(failed_rows, previous, today):
    """Return a Telegram message listing bets with no price, or None if this
    exact list was already reported today."""
    tickers = sorted({(row["ticker"] or "").strip().upper() for row in failed_rows if row["ticker"]})
    if not tickers:
        return None
    if previous and previous.get("date") == today and previous.get("tickers") == tickers:
        return None
    return tickers, (
        "StockMonitor: could not fetch prices for "
        f"{len(tickers)} open bet(s), so they were NOT checked for target or stop loss:\n"
        + ", ".join(tickers)
    )


def run_market_check(config):
    logger.info("=" * 60)
    logger.info("Scheduled market check started")

    rows, failed_rows = refresh_open_position_prices(config, context="Market check")
    if failed_rows:
        logger.warning("Market check price refresh had failures: failed=%s", len(failed_rows))
    try:
        record_portfolio_snapshot()
    except Exception:
        logger.exception("Portfolio performance snapshot failed")

    stop_loss_enabled = get_bool(config, "ALERTS", "ENABLE_STOPLOSS", True)
    state = load_state()
    previous_alerts = state.get("alerts") or {}
    today = datetime.now().date().isoformat()
    # Keep entries for bets whose price fetch failed this time, drop closed bets.
    open_keys = {str(row["id"]) for row in rows} | {str(row["id"]) for row in failed_rows}
    alert_state = {key: value for key, value in previous_alerts.items() if key in open_keys}

    results = []
    for row in rows:
        result = evaluate(row, stop_loss_enabled=stop_loss_enabled)
        if result is None:
            logger.warning("Alert evaluation skipped: bet_no=%s ticker=%s", row["bet_no"], row["ticker"])
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

        key = alert_key(result)
        previous = alert_state.get(key)
        sent = False
        if should_send_alert(previous, result.status, today):
            message = format_whatsapp_message(result)
            logger.info("PORTFOLIO ALERT\n%s", message)
            send_alert(config, result, message)
            sent = True
        elif ALERT_LEVELS.get(result.status, 0):
            logger.info(
                "Alert already sent today: bet_no=%s ticker=%s status=%s",
                result.bet_no,
                result.ticker,
                result.status,
            )
        alert_state[key] = next_alert_state(previous, result.status, today, sent)

    state = load_state()
    state["alerts"] = alert_state
    failed_alert = failed_price_alert_message(failed_rows, state.get("price_failure_alert"), today)
    if failed_alert:
        tickers, message = failed_alert
        logger.warning("Price fetch failed; bets not checked: %s", ", ".join(tickers))
        if send_telegram(config, message):
            state["price_failure_alert"] = {"date": today, "tickers": tickers}
    save_state(state)

    try:
        generate_report_snapshots(config)
    except Exception:
        logger.exception("Report snapshot generation failed")

    try:
        known_prices = {
            (row["ticker"] or "").strip().upper(): row["current_price"]
            for row in rows
            if row["current_price"] is not None
        }
        run_stock_price_alert_check(config, known_prices=known_prices)
    except Exception:
        logger.exception("Stock price alert check failed")

    market_check_at = datetime.now().replace(microsecond=0).isoformat()
    write_heartbeat(status="ok", last_market_check_at=market_check_at)
    logger.info("Scheduled market check completed: evaluated=%s", len(results))
    return results


def configured_market_holidays(config):
    """Return configured exchange holidays as ISO date strings."""
    raw = config.get("MARKET", "HOLIDAYS", fallback="")
    return {
        value.strip()
        for value in raw.replace("\n", ",").split(",")
        if value.strip()
    }


def is_trading_day(config, value=None):
    """True only for weekdays that are not configured exchange holidays."""
    value = value or datetime.now()
    day = value.date() if isinstance(value, datetime) else value
    return day.weekday() < 5 and day.isoformat() not in configured_market_holidays(config)


def run_eod_fund_nav_if_due(config, now=None):
    """Record each fund's closing NAV once per trading day, after REPORT_TIME."""
    now = now or datetime.now()
    today = now.date().isoformat()
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    if now.time() < report_time or not is_trading_day(config, now):
        return None
    state = load_state()
    if state.get("fund_nav_date") == today:
        return None
    try:
        from fund_management import record_eod_nav_snapshots

        recorded = record_eod_nav_snapshots()
    except Exception:
        logger.exception("End-of-day fund NAV recording failed")
        return None
    logger.info("End-of-day fund NAV recorded: %s", recorded)
    state = load_state()
    state["fund_nav_date"] = today
    save_state(state)
    return recorded


def run_daily_backup_if_due(config, now=None):
    """Back up the database once a day, after REPORT_TIME (or at the first
    scheduler cycle after it, if the laptop was off at that time)."""
    now = now or datetime.now()
    today = now.date().isoformat()
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    if now.time() < report_time:
        return None
    state = load_state()
    if state.get("backup_date") == today:
        return None
    keep = config.getint("BACKUP", "KEEP", fallback=30)
    copy_dir = config.get("BACKUP", "COPY_DIR", fallback="").strip() or None
    try:
        path = backup_database(keep=keep, copy_dir=copy_dir)
    except Exception:
        logger.exception("Daily database backup failed")
        if state.get("backup_failed_alert_date") != today:
            send_telegram(config, "StockMonitor: daily database backup FAILED. Check the scheduler log.")
            state["backup_failed_alert_date"] = today
            save_state(state)
        return None
    state = load_state()
    state["backup_date"] = today
    state["backup_path"] = str(path)
    save_state(state)
    return path


def run_eod_email_if_due(config, force=False):
    now = datetime.now()
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    market_end = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))

    if not force:
        if not is_trading_day(config, now):
            logger.info("EOD email not due: non-trading day date=%s", today)
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

    refreshed_rows, failed_rows = refresh_open_position_prices(config, context="EOD")
    if failed_rows:
        logger.warning("EOD price refresh had failures; continuing with stored DB prices for failed rows: failed=%s", len(failed_rows))
    try:
        record_portfolio_snapshot()
    except Exception:
        logger.exception("Portfolio performance snapshot failed")

    rows = scheduler_positions(config)
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

    today_date = now.date()

    try:
        record_stock_booking_snapshot(rows, today_date)
    except Exception:
        logger.exception("Stock booking simulation snapshot failed")

    def parse_date(value):
        return parse_shared_date(value)

    def display_date(value):
        return display_shared_date(value)

    def safe(value):
        return escape(str(value or "-"))

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
        return format_indian_money(value)

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
        if row["current_price"] >= row["buy_price"] and remaining_pct <= 5:
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
                generate_report_snapshots(config)
                published = publish_masked_report(f"EOD report {today}")
                if not published:
                    logger.error("EOD publish did not complete successfully")
            except Exception:
                logger.exception("EOD publish failed")


def is_market_time(config, now=None):
    now = now or datetime.now()
    if not is_trading_day(config, now):
        return False
    start = parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15"))
    end = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))
    return start <= now.time() <= end


def next_trading_day_at(config, target_time, now=None):
    now = now or datetime.now()
    candidate = datetime.combine(now.date(), target_time)
    if candidate <= now:
        candidate += timedelta(days=1)
    while not is_trading_day(config, candidate):
        candidate += timedelta(days=1)
    return candidate


def next_scheduler_wakeup(config, next_market_check_at, now=None):
    now = now or datetime.now()
    start_time = parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15"))
    end_time = parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30"))
    report_time = parse_hhmm(config.get("MARKET", "REPORT_TIME", fallback="15:35"))
    start_at = datetime.combine(now.date(), start_time)
    end_at = datetime.combine(now.date(), end_time)
    report_at = datetime.combine(now.date(), report_time)

    candidates = []
    if is_trading_day(config, now):
        if now < start_at:
            candidates.append(start_at)
        elif start_at <= now <= end_at and next_market_check_at:
            candidates.append(next_market_check_at)

        if now < report_at:
            candidates.append(report_at)

    candidates.append(next_trading_day_at(config, start_time, now))

    next_event = min(candidate for candidate in candidates if candidate > now)
    seconds = max((next_event - now).total_seconds(), 1)
    # Keep sleeps short so a laptop wake/resume does not leave the scheduler
    # stuck waiting on yesterday's long timeout.
    return min(seconds, 60)


def run_once(force=False):
    # Published snapshots remain the administrator's portfolio only; individual
    # users receive their scoped report through the application.
    admin = get_user_by_username(os.getenv("ADMIN_USERNAME", "admin"))
    set_current_user_id(admin["id"] if admin else None)
    config = load_config()
    logger.info("Scheduler run_once force=%s", force)
    write_heartbeat(status="starting")
    maybe_alert_stale_scheduler(config)
    if force or is_market_time(config):
        run_market_check(config)
    else:
        logger.info("Outside Indian market hours. Skipping market check.")
        write_heartbeat(status="idle")
    run_eod_email_if_due(config, force=force)
    run_eod_fund_nav_if_due(config)
    run_daily_backup_if_due(config)


def run_forever():
    admin = get_user_by_username(os.getenv("ADMIN_USERNAME", "admin"))
    set_current_user_id(admin["id"] if admin else None)
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
            write_heartbeat(status="running")
            maybe_alert_stale_scheduler(config)
            start = today_at(parse_hhmm(config.get("MARKET", "START_TIME", fallback="09:15")))
            end = today_at(parse_hhmm(config.get("MARKET", "END_TIME", fallback="15:30")))

            if next_market_check_at is None or next_market_check_at.date() != now.date():
                next_market_check_at = start if now < start else now

            if is_trading_day(config, now) and start <= now <= end and now >= next_market_check_at:
                logger.info("Market cycle due: scheduled_for=%s", next_market_check_at.strftime("%Y-%m-%d %H:%M:%S"))
                run_market_check(config)
                next_market_check_at = datetime.now() + timedelta(minutes=interval)
                logger.info("Next market cycle scheduled_for=%s", next_market_check_at.strftime("%Y-%m-%d %H:%M:%S"))

            run_eod_email_if_due(config, force=False)
            run_eod_fund_nav_if_due(config)
            run_daily_backup_if_due(config)
            sleep_seconds = next_scheduler_wakeup(config, next_market_check_at)
            write_heartbeat(status="sleeping")
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

    warn_if_not_ist()
    try:
        if args.once:
            run_once(force=args.force)
        else:
            run_forever()
    finally:
        instance_lock.release()
