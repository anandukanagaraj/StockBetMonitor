import calendar
import configparser
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_user_by_id
from db import create_position, create_stock_price_alert, create_stock_trade_group, create_user, delete_paper_stock_trade_group, delete_position, delete_stock_price_alert, get_default_stock_trade_group, get_position, get_stock_price_alert, get_stock_trade_group, get_user_by_username, init_db, list_portfolio_performance_snapshots, list_positions, list_positions_for_management, list_stock_booking_simulation_snapshots, list_stock_price_alerts, list_stock_trade_groups, list_users, metrics, next_bet_no, next_stock_bet_numbers, record_stock_booking_simulation_snapshot, record_stock_stop_recovery_audit, set_stock_trade_group_active, set_user_active, set_user_password, update_current_price, update_position, update_stock_price_alert, update_stock_trade_group, update_user_profile
from options_db import create_option_strategy, create_trade_group, delete_option_strategy, delete_trade_group, get_option_strategy, get_trade_group, init_options_db, list_option_strategies, list_option_strategies_missing_stock_move, list_trade_groups, next_option_bet_numbers, next_strategy_no, set_trade_group_active, update_option_stock_move, update_option_strategy, update_trade_group
from services.date_utils import display_date as display_shared_date, normalized_buy_date_map, parse_date as parse_shared_date, position_identity
from services.excel_exporter import export_positions_to_excel
from services.index_service import build_dashboard_review_summary, build_index_membership_report
from services.option_charges import (
    add_costs,
    calculated_option_max_risk,
    empty_costs,
    net_after_costs,
    option_leg_type,
    option_strategy_costs,
    option_strategy_risk_capital,
    single_leg_amount_risked,
    single_leg_simulation_costs,
    stock_equivalent_notional,
    stock_simulation_costs,
    stock_simulation_label,
    stock_simulation_pl_from_move,
    stock_simulation_side,
)
from services.number_format import format_indian_compact_money, format_indian_decimal, format_indian_inr, format_indian_money
from services.portfolio_analytics import build_portfolio_analytics
from services.report_cleanup import cleanup_old_reports, latest_report_path, update_report_manifest
from services.scheduler_health import (
    heartbeat_age_minutes,
    is_heartbeat_stale,
    market_check_age_minutes,
    read_heartbeat,
    scheduler_process_running,
    HEARTBEAT_PATH,
)
from services.stock_profit_booking_simulator import simulate_stock_profit_booking
from services.historical_portfolio_simulator import simulate_funding_sustainability, simulate_historical_portfolio
from services.v1_report_generator import ReportGenerator
from services.yahoo_service import historical_stock_move_pct, latest_price
from user_context import reset_current_user_id, set_current_user_id
from fund_management import register_fund_management


app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STOCK_MOVE_FAILURES_PATH = BASE_DIR / "data" / "stock_move_failures.json"
STOCK_MOVE_FAILURE_VERSION = "yahoo-nse-v1"
REPORTS_DIR = BASE_DIR / "reports"
DEFAULT_DEV_SECRET = "stock-monitor-v3-dev-change-before-hosting"
APP_BIND_HOST = os.getenv("APP_BIND_HOST", "127.0.0.1")
IS_LOCAL_BIND = APP_BIND_HOST.strip().lower() in {"127.0.0.1", "localhost", "::1"}

_secret_key = os.getenv("APP_SECRET_KEY")
if not _secret_key:
    if IS_LOCAL_BIND:
        _secret_key = DEFAULT_DEV_SECRET
    else:
        raise RuntimeError("APP_SECRET_KEY must be set when APP_BIND_HOST is not localhost.")
app.secret_key = _secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
if os.getenv("TRUST_PROXY", "false").strip().lower() == "true":
    # Behind one reverse proxy (nginx/Caddy/Traefik): trust its X-Forwarded-* headers
    # so request.remote_addr, https redirects and url_for use the real client values.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
init_db()
init_options_db()


def setup_logging():
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if any(getattr(handler, "baseFilename", None) == str(log_file) for handler in app.logger.handlers):
        return

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    app.logger.setLevel(logging.DEBUG)
    app.logger.addHandler(file_handler)
    logging.getLogger("werkzeug").addHandler(file_handler)
    logging.getLogger("werkzeug").setLevel(logging.INFO)


setup_logging()


STATUS_OPTIONS = ["Open", "Closed"]
OPTION_STATUS_OPTIONS = ["Open", "Closed"]
STOCK_ALERT_DIRECTIONS = {
    "ABOVE": "Price reaches or goes above",
    "BELOW": "Price reaches or goes below",
}
STOCK_ALERT_CHANNELS = {
    "BOTH": "WhatsApp, then Telegram fallback",
    "WHATSAPP": "WhatsApp only",
    "TELEGRAM": "Telegram only",
}
TRADE_GROUP_TYPES = {
    "LIVE": "Live",
    "PAPER": "Paper Trade",
}
OPTION_STRATEGY_TYPES = [
    "Long Call",
    "Long Put",
    "Bull Call Spread",
    "Bear Put Spread",
    "Bull Put Spread",
    "Bear Call Spread",
    "Debit Spread",
    "Credit Spread",
    "Iron Condor",
    "Iron Butterfly",
    "Straddle",
    "Strangle",
    "Custom Multi-leg",
]
DEFAULT_WHATIF_TRADE_LIMIT = 100
DEFAULT_STOCK_MOVE_BACKFILL_LIMIT = 5
MAX_PRICE_VALUE = 1_000_000
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")


def configured_int(section, key, default, env_name=None, minimum=None):
    raw_value = os.getenv(env_name) if env_name else None
    if raw_value is None:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        raw_value = config.get(section, key, fallback=None)
    try:
        value = int(raw_value) if raw_value not in {None, ""} else int(default)
    except (TypeError, ValueError):
        app.logger.warning("Invalid integer config %s.%s=%r; using %s", section, key, raw_value, default)
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    return value


def configured_float(section, key, default, env_name=None, minimum=None, maximum=None):
    raw_value = os.getenv(env_name) if env_name else None
    if raw_value is None:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        raw_value = config.get(section, key, fallback=None)
    try:
        value = float(raw_value) if raw_value not in {None, ""} else float(default)
    except (TypeError, ValueError):
        app.logger.warning("Invalid float config %s.%s=%r; using %s", section, key, raw_value, default)
        value = float(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


WHATIF_TRADE_LIMIT = configured_int(
    "OPTIONS",
    "WHATIF_TRADE_LIMIT",
    DEFAULT_WHATIF_TRADE_LIMIT,
    env_name="WHATIF_TRADE_LIMIT",
    minimum=1,
)
STOCK_MOVE_BACKFILL_LIMIT = configured_int(
    "OPTIONS",
    "STOCK_MOVE_BACKFILL_LIMIT",
    DEFAULT_STOCK_MOVE_BACKFILL_LIMIT,
    env_name="STOCK_MOVE_BACKFILL_LIMIT",
    minimum=0,
)
STCG_TAX_RATE = configured_float("OPTIONS", "STCG_TAX_RATE", 20, env_name="STCG_TAX_RATE", minimum=0, maximum=100)
BUSINESS_INCOME_TAX_RATE = configured_float(
    "OPTIONS",
    "BUSINESS_INCOME_TAX_RATE",
    30,
    env_name="BUSINESS_INCOME_TAX_RATE",
    minimum=0,
    maximum=100,
)
REPORT_PRICE_REFRESH_MAX_AGE_MINUTES = configured_int(
    "APP",
    "REPORT_PRICE_REFRESH_MAX_AGE_MINUTES",
    30,
    env_name="REPORT_PRICE_REFRESH_MAX_AGE_MINUTES",
    minimum=0,
)
REPORT_KEEP_DAYS = configured_int("APP", "REPORT_KEEP_DAYS", 14, env_name="REPORT_KEEP_DAYS", minimum=1)
SCHEDULER_STALE_MINUTES = configured_int(
    "APP",
    "SCHEDULER_STALE_MINUTES",
    45,
    env_name="SCHEDULER_STALE_MINUTES",
    minimum=5,
)
APP_PORT = configured_int("APP", "PORT", 5002, env_name="PORT", minimum=1)
_api_config = configparser.ConfigParser()
_api_config.read(CONFIG_PATH)
API_KEY = os.getenv("PORTFOLIO_API_KEY")
if API_KEY is None:
    API_KEY = _api_config.get("API", "PORTFOLIO_API_KEY", fallback="").strip()
# Whose portfolio the key-protected API returns.
API_USERNAME = (
    os.getenv("PORTFOLIO_API_USER")
    or _api_config.get("API", "PORTFOLIO_API_USER", fallback="").strip()
    or ADMIN_USERNAME
)
DEFAULT_ADMIN_PASSWORD = "admin123"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
if ADMIN_PASSWORD_HASH:
    ACTIVE_PASSWORD_HASH = ADMIN_PASSWORD_HASH
else:
    ACTIVE_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)
    if IS_LOCAL_BIND:
        app.logger.warning("Using development admin password. Set ADMIN_PASSWORD_HASH before hosting.")
    else:
        raise RuntimeError("ADMIN_PASSWORD_HASH must be set when APP_BIND_HOST is not localhost.")


def validate_security_config():
    if app.secret_key == DEFAULT_DEV_SECRET and IS_LOCAL_BIND:
        app.logger.warning("Using development APP_SECRET_KEY. Set APP_SECRET_KEY before hosting beyond localhost.")
    if not API_KEY and not IS_LOCAL_BIND:
        app.logger.warning("PORTFOLIO_API_KEY is not set. The portfolio API will reject non-local requests.")


validate_security_config()


def is_authenticated():
    return session.get("user_id") is not None


def is_admin():
    return session.get("is_admin") is True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login"))
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def set_request_user_context():
    user_id = session.get("user_id")
    if user_id is not None:
        # Re-check the account on every request so disabling a user, or
        # removing admin rights, takes effect immediately.
        user = get_user_by_id(user_id)
        if not user or not user["is_active"]:
            app.logger.warning("Session ended for disabled or missing user: user_id=%s", user_id)
            session.clear()
            user_id = None
        else:
            session["is_admin"] = bool(user["is_admin"])
    set_current_user_id(user_id)


LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60
_login_failures = {}
_login_failures_lock = threading.Lock()


def _login_key(username):
    return (username.strip().lower(), request.remote_addr or "")


def _recent_login_failures(key, now):
    return [stamp for stamp in _login_failures.get(key, []) if now - stamp < LOGIN_LOCK_SECONDS]


def login_locked(username, now=None):
    now = now or time.time()
    with _login_failures_lock:
        recent = _recent_login_failures(_login_key(username), now)
        _login_failures[_login_key(username)] = recent
        return len(recent) >= LOGIN_MAX_FAILURES


def record_login_failure(username, now=None):
    now = now or time.time()
    with _login_failures_lock:
        key = _login_key(username)
        _login_failures[key] = _recent_login_failures(key, now) + [now]


def clear_login_failures(username):
    with _login_failures_lock:
        _login_failures.pop(_login_key(username), None)


def safe_next_url(value):
    """Only allow redirects to paths on this site (no other hosts)."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return None
    return value


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf():
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        app.logger.warning("CSRF validation failed: endpoint=%s path=%s", request.endpoint, request.path)
        abort(400, "Invalid security token. Please refresh and try again.")


@app.context_processor
def inject_security_helpers():
    return {
        "csrf_token": csrf_token,
        "is_authenticated": is_authenticated,
        "is_admin": is_admin,
        "current_username": lambda: session.get("username"),
        "password_change_required": lambda: bool(session.get("must_change_password")),
        "compact_money": compact_money,
    }


def money(value):
    return format_indian_money(value)


def compact_money(value):
    return format_indian_compact_money(value)


def number(value):
    return format_indian_decimal(value)


def pct(value):
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def rate(value):
    if value is None:
        return "-"
    return f"{value:.2f}%"


def pl_class(value):
    if value is None:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def option_strategy_return_pct(row):
    if row.get("strategy_pl") is None:
        return None
    capital = option_strategy_risk_capital(row)
    if not capital:
        return None
    return (row["strategy_pl"] or 0) / capital * 100


def best_scenario_label(scenarios):
    available = [
        (label, value)
        for label, value in scenarios
        if value is not None
    ]
    if not available:
        return "-"
    return max(available, key=lambda item: item[1])[0]


def option_bet_comparison(strategy, legs):
    long_call_values = [
        leg["achieved_pl"]
        for leg in legs
        if (leg["action"] or "").upper() == "BUY"
        and option_leg_type(leg) == "CE"
        and leg["achieved_pl"] is not None
    ]
    long_put_values = [
        leg["achieved_pl"]
        for leg in legs
        if (leg["action"] or "").upper() == "BUY"
        and option_leg_type(leg) == "PE"
        and leg["achieved_pl"] is not None
    ]
    actual = strategy["strategy_pl"]
    long_call = sum(long_call_values) if long_call_values else None
    long_put = sum(long_put_values) if long_put_values else None
    stock_notional = stock_equivalent_notional(strategy["strategy_type"], legs)
    long_stock = stock_simulation_pl_from_move(strategy["stock_move_pct"], strategy["strategy_type"], stock_notional)
    actual_costs = option_strategy_costs(legs)
    long_call_costs = single_leg_simulation_costs(legs, "CE")
    long_put_costs = single_leg_simulation_costs(legs, "PE")
    long_stock_costs = stock_simulation_costs(strategy["stock_move_pct"], strategy["strategy_type"], stock_notional)
    actual_net = net_after_costs(actual, actual_costs)
    scenarios = [
        ("Actual Strategy", actual, actual_costs, option_strategy_risk_capital(strategy)),
        ("Long Call Only", long_call, long_call_costs, single_leg_amount_risked(legs, "CE")),
        ("Long Put Only", long_put, long_put_costs, single_leg_amount_risked(legs, "PE")),
        (stock_simulation_label(strategy["strategy_type"]), long_stock, long_stock_costs, stock_notional),
    ]
    return {
        "scenarios": [
            {
                "label": label,
                "pl": value,
                "capital": capital,
                "costs": costs,
                "brokerage": costs.get("brokerage") if costs else None,
                "stt": costs.get("stt") if costs else None,
                "charges": costs.get("total") if costs else None,
                "net": net_after_costs(value, costs),
                "diff": (
                    None
                    if net_after_costs(value, costs) is None or actual_net is None
                    else net_after_costs(value, costs) - actual_net
                ),
            }
            for label, value, costs, capital in scenarios
        ],
        "stock_notional": stock_notional,
        "best": best_scenario_label(
            [(label, net_after_costs(value, costs)) for label, value, costs, capital in scenarios]
        ),
    }


def stock_move_failure_key(row):
    return f"{STOCK_MOVE_FAILURE_VERSION}|{row['id']}|{row['underlying']}|{row['buy_date']}|{row['sell_date']}"


def load_stock_move_failures():
    if not STOCK_MOVE_FAILURES_PATH.exists():
        return {}
    try:
        return json.loads(STOCK_MOVE_FAILURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        app.logger.exception("Could not read stock move failure cache: %s", STOCK_MOVE_FAILURES_PATH)
        return {}


def save_stock_move_failures(failures):
    STOCK_MOVE_FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOCK_MOVE_FAILURES_PATH.write_text(json.dumps(failures, indent=2, sort_keys=True), encoding="utf-8")


def remember_stock_move_failure(row, exc):
    failures = load_stock_move_failures()
    failures[stock_move_failure_key(row)] = {
        "underlying": row["underlying"],
        "buy_date": row["buy_date"],
        "sell_date": row["sell_date"],
        "error": str(exc),
        "failed_at": datetime.now().replace(microsecond=0).isoformat(),
    }
    save_stock_move_failures(failures)


def backfill_missing_option_stock_moves():
    if STOCK_MOVE_BACKFILL_LIMIT <= 0:
        app.logger.info("Option stock move backfill disabled: limit=%s", STOCK_MOVE_BACKFILL_LIMIT)
        return 0

    rows = list_option_strategies_missing_stock_move(limit=STOCK_MOVE_BACKFILL_LIMIT)
    if not rows:
        return 0

    updated = 0
    skipped_after_failure = 0
    failures = load_stock_move_failures()
    for row in rows:
        if stock_move_failure_key(row) in failures:
            skipped_after_failure += 1
            app.logger.debug(
                "Skipping stock move backfill after previous failure: id=%s underlying=%s buy_date=%s sell_date=%s",
                row["id"],
                row["underlying"],
                row["buy_date"],
                row["sell_date"],
            )
            continue
        try:
            move_pct = historical_stock_move_pct(row["underlying"], row["buy_date"], row["sell_date"])
            update_option_stock_move(row["id"], move_pct)
            updated += 1
            app.logger.info(
                "Option stock move backfilled: id=%s underlying=%s buy_date=%s sell_date=%s move_pct=%s",
                row["id"],
                row["underlying"],
                row["buy_date"],
                row["sell_date"],
                move_pct,
            )
        except Exception as exc:
            remember_stock_move_failure(row, exc)
            app.logger.exception(
                "Could not backfill option stock move: id=%s underlying=%s buy_date=%s sell_date=%s",
                row["id"],
                row["underlying"],
                row["buy_date"],
                row["sell_date"],
            )
    app.logger.info(
        "Option stock move backfill completed: checked=%s updated=%s skipped_after_failure=%s limit=%s",
        len(rows),
        updated,
        skipped_after_failure,
        STOCK_MOVE_BACKFILL_LIMIT,
    )
    return updated


def display_buy_date(row, normalized_dates=None):
    normalized_dates = normalized_dates or {}
    value = normalized_dates.get(position_identity(row)) or row["buy_date"]
    return display_shared_date(value)


def realized_pl(row):
    if row["sell_price"] is None:
        return None
    return (row["sell_price"] - row["buy_price"]) * row["qty"]


def realized_pl_pct(row):
    if row["sell_price"] is None or row["buy_price"] in (None, 0):
        return None
    return ((row["sell_price"] - row["buy_price"]) / row["buy_price"]) * 100


def is_closed_position(row):
    status = (row["status"] or "").strip().lower()
    return row["sell_price"] is not None or row["sell_date"] is not None or status in {"closed", "target hit", "stop loss hit", "sold"}


def management_position_status(row):
    if is_closed_position(row):
        return {"label": "Closed", "class": "closed", "sort": 99}

    current_price = row["current_price"]
    target_price = row["target_price"]
    stop_loss = row["stop_loss"]

    if current_price is None:
        return {"label": "Price Missing", "class": "closed", "sort": 3}

    if stop_loss is not None and current_price <= stop_loss:
        return {"label": "Stop Loss Hit", "class": "danger", "sort": 0}

    if target_price:
        if current_price >= target_price:
            return {"label": "Target Hit", "class": "success", "sort": 1}
        remaining_pct = ((target_price - current_price) / target_price) * 100
        if current_price >= row["buy_price"] and remaining_pct <= 5:
            return {"label": "Near Target", "class": "warning", "sort": 2}

    return {"label": "Hold", "class": "", "sort": 4}


def management_status_sort_key(row):
    status = management_position_status(row)
    target_price = row["target_price"]
    current_price = row["current_price"]
    remaining_pct = 999999
    if target_price and current_price is not None:
        remaining_pct = ((target_price - current_price) / target_price) * 100
    return (status["sort"], remaining_pct, (row["ticker"] or "").lower(), row["bet_no"] or 0)


def management_newest_sort_key(row):
    return (row["bet_no"] or 0, row["id"] or 0)


def build_report(positions):
    open_positions = [row for row in positions if not is_closed_position(row)]
    closed_positions = [row for row in positions if is_closed_position(row)]

    winners = []
    losers = []
    for row in closed_positions:
        profit = realized_pl(row)
        pct = realized_pl_pct(row)
        if profit is None or pct is None:
            continue
        item = {"ticker": row["ticker"], "profit": profit, "pct": pct}
        if profit >= 0:
            winners.append(item)
        else:
            losers.append(item)

    near_targets = []
    for row in open_positions:
        if row["current_price"] is None or row["target_price"] in (None, 0):
            continue
        remaining = ((row["target_price"] - row["current_price"]) / row["target_price"]) * 100
        if row["current_price"] >= row["buy_price"] and remaining <= 5:
            near_targets.append(
                {
                    "ticker": row["ticker"],
                    "current": row["current_price"],
                    "target": row["target_price"],
                    "remaining": remaining,
                    "status": "Target Hit" if remaining <= 0 else "Near Target",
                }
            )

    recent_closed = sorted(
        closed_positions,
        key=lambda row: row["sell_date"] or row["buy_date"] or "",
        reverse=True,
    )

    return {
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "top_winners": sorted(winners, key=lambda item: item["pct"], reverse=True)[:5],
        "top_losers": sorted(losers, key=lambda item: item["pct"])[:5],
        "near_targets": sorted(near_targets, key=lambda item: item["remaining"])[:5],
        "recent_closed": recent_closed[:6],
    }


def stock_dashboard_metrics(positions):
    open_positions = [row for row in positions if not is_closed_position(row)]
    closed_positions = [row for row in positions if is_closed_position(row)]
    closed_with_sell = [row for row in closed_positions if row["sell_price"] is not None]
    realized_profit = sum(realized_pl(row) or 0 for row in closed_with_sell)
    open_capital = sum((row["buy_price"] or 0) * (row["qty"] or 0) for row in open_positions)
    returns = [realized_pl_pct(row) for row in closed_with_sell if realized_pl_pct(row) is not None]
    winners = [row for row in closed_with_sell if (realized_pl(row) or 0) > 0]
    losers = [row for row in closed_with_sell if (realized_pl(row) or 0) < 0]
    price_missing = len([row for row in open_positions if row["current_price"] is None])
    target_missing = len([row for row in open_positions if row["target_price"] is None])
    return {
        "total": len(positions),
        "open": len(open_positions),
        "closed": len(closed_positions),
        "realized_pl": realized_profit,
        "open_capital": open_capital,
        "win_rate": (len(winners) / len(closed_with_sell) * 100) if closed_with_sell else None,
        "avg_return": (sum(returns) / len(returns)) if returns else None,
        "wins": len(winners),
        "losses": len(losers),
        "price_missing": price_missing,
        "target_missing": target_missing,
    }


def api_float(value):
    return round(float(value), 2) if value is not None else None


def api_pct(value):
    return round(float(value), 4) if value is not None else None


def row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def require_portfolio_api_key():
    remote = (request.remote_addr or "").strip()
    is_local_request = remote in {"127.0.0.1", "::1"}
    if not API_KEY:
        # Without a key, only allow requests on a localhost-only install. When
        # hosted, a reverse proxy can make every request look local.
        if is_local_request and IS_LOCAL_BIND:
            return None
        return jsonify({"error": "Unauthorized"}), 401
    supplied = request.headers.get("X-API-Key")
    if not supplied or not secrets.compare_digest(supplied, API_KEY):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def stock_actual_pl_payload(positions):
    rows = []
    realized_pl_total = 0
    unrealized_pl_total = 0
    closed_with_sell = 0
    winners = 0
    losers = 0
    capital_total = 0
    open_capital = 0
    realized_capital = 0
    returns = []

    for row in positions:
        closed = is_closed_position(row)
        qty = row["qty"] or 0
        buy_price = row["buy_price"] or 0
        current_price = row["current_price"]
        sell_price = row["sell_price"]
        capital = buy_price * qty
        realized_value = realized_pl(row) if closed and sell_price is not None else None
        unrealized_value = ((current_price - buy_price) * qty) if not closed and current_price is not None else None
        actual_value = realized_value if realized_value is not None else unrealized_value
        pct_value = realized_pl_pct(row) if realized_value is not None else (
            ((current_price - buy_price) / buy_price * 100)
            if not closed and current_price is not None and buy_price
            else None
        )

        capital_total += capital
        if closed and realized_value is not None:
            closed_with_sell += 1
            realized_pl_total += realized_value
            realized_capital += capital
            returns.append(pct_value)
            if realized_value > 0:
                winners += 1
            elif realized_value < 0:
                losers += 1
        elif not closed:
            open_capital += capital
            if unrealized_value is not None:
                unrealized_pl_total += unrealized_value

        rows.append(
            {
                "id": row["id"],
                "bet_no": row["bet_no"],
                "trade_group": row_get(row, "trade_group_name"),
                "trade_group_code": row_get(row, "trade_group_code"),
                "ticker": row["ticker"],
                "status": "Closed" if closed else "Open",
                "buy_date": row["buy_date"],
                "sell_date": row["sell_date"],
                "qty": api_float(qty),
                "buy_price": api_float(buy_price),
                "current_price": api_float(current_price),
                "sell_price": api_float(sell_price),
                "capital": api_float(capital),
                "realized_pl": api_float(realized_value),
                "unrealized_pl": api_float(unrealized_value),
                "actual_pl": api_float(actual_value),
                "pl_pct": api_pct(pct_value),
            }
        )

    return {
        "summary": {
            "total_bets": len(positions),
            "open_bets": len([row for row in positions if not is_closed_position(row)]),
            "closed_bets": len([row for row in positions if is_closed_position(row)]),
            "closed_with_sell_price": closed_with_sell,
            "realized_pl": api_float(realized_pl_total),
            "unrealized_pl": api_float(unrealized_pl_total),
            "actual_pl": api_float(realized_pl_total + unrealized_pl_total),
            "capital": api_float(capital_total),
            "open_capital": api_float(open_capital),
            "realized_capital": api_float(realized_capital),
            "wins": winners,
            "losses": losers,
            "win_rate": api_pct((winners / closed_with_sell * 100) if closed_with_sell else None),
            "avg_realized_return": api_pct((sum(returns) / len(returns)) if returns else None),
        },
        "bets": rows,
    }


def option_actual_pl_payload(strategies):
    rows = []
    closed = []
    open_rows = []
    realized_pl_total = 0
    risk_capital_total = 0
    winners = 0
    losers = 0

    for row in strategies:
        is_closed_strategy = (row["status"] or "").lower() == "closed"
        risk_capital = option_strategy_risk_capital(row)
        display_return = option_strategy_return_pct(row)
        strategy_pl = row["strategy_pl"] if is_closed_strategy else None
        if is_closed_strategy:
            closed.append(row)
            realized_pl_total += strategy_pl or 0
            if strategy_pl is not None and risk_capital:
                risk_capital_total += risk_capital
            if (strategy_pl or 0) > 0:
                winners += 1
            elif (strategy_pl or 0) < 0:
                losers += 1
        else:
            open_rows.append(row)

        rows.append(
            {
                "id": row["id"],
                "bet_no": row["bet_no"] or row["strategy_no"],
                "strategy_no": row["strategy_no"],
                "trade_group": row.get("trade_group_name"),
                "trade_group_code": row.get("trade_group_code"),
                "strategy_type": row["strategy_type"],
                "underlying": row["underlying"],
                "status": row["status"],
                "buy_date": row["buy_date"],
                "sell_date": row["sell_date"],
                "legs": row.get("leg_count"),
                "risk_capital": api_float(risk_capital),
                "trade_capital": api_float(row.get("total_trade_capital")),
                "strategy_pl": api_float(row["strategy_pl"]),
                "actual_pl": api_float(strategy_pl),
                "pl_pct": api_pct(display_return),
            }
        )

    return {
        "summary": {
            "total_bets": len(strategies),
            "open_bets": len(open_rows),
            "closed_bets": len(closed),
            "realized_pl": api_float(realized_pl_total),
            "actual_pl": api_float(realized_pl_total),
            "risk_capital": api_float(risk_capital_total),
            "wins": winners,
            "losses": losers,
            "win_rate": api_pct((winners / len(closed) * 100) if closed else None),
            "avg_return": api_pct((realized_pl_total / risk_capital_total * 100) if risk_capital_total else None),
        },
        "bets": rows,
    }


def realized_pl_by_year(positions, strategies):
    for row in strategies:
        row["display_pl_pct"] = option_strategy_return_pct(row)
    transactions = build_pl_transactions(positions, strategies, source_filter="all")
    by_year = {}
    for item in transactions:
        year = str(item["date"].year)
        bucket = by_year.setdefault(
            year,
            {
                "year": int(year),
                "stock_realized_pl": 0,
                "option_realized_pl": 0,
                "realized_pl": 0,
                "stock_bets": 0,
                "option_bets": 0,
                "wins": 0,
                "losses": 0,
            },
        )
        source = (item.get("source") or "").lower()
        pl_value = item.get("pl") or 0
        if source == "stock":
            bucket["stock_realized_pl"] += pl_value
            bucket["stock_bets"] += 1
        elif source == "option":
            bucket["option_realized_pl"] += pl_value
            bucket["option_bets"] += 1
        bucket["realized_pl"] += pl_value
        if pl_value > 0:
            bucket["wins"] += 1
        elif pl_value < 0:
            bucket["losses"] += 1

    return {
        str(bucket["year"]): {
            "stock_realized_pl": api_float(bucket["stock_realized_pl"]),
            "option_realized_pl": api_float(bucket["option_realized_pl"]),
        }
        for bucket in sorted(by_year.values(), key=lambda item: item["year"], reverse=True)
    }


def recent_stock_activity(positions, limit=5):
    normalized_buy_dates = normalized_buy_date_map(positions)

    def activity_date(row):
        if is_closed_position(row) and row["sell_date"]:
            return parse_shared_date(row["sell_date"])
        return normalized_buy_dates.get(position_identity(row)) or parse_shared_date(row["buy_date"])

    rows = sorted(
        positions,
        key=lambda row: (
            activity_date(row) or date.min,
            row["created_at"] or "",
            row["updated_at"] or "",
            row["id"],
        ),
        reverse=True,
    )
    activity = []
    for row in rows[:limit]:
        closed = is_closed_position(row)
        pl_value = realized_pl(row) if closed else None
        row_date = activity_date(row)
        activity.append(
            {
                "label": f"Bet {row['bet_no'] or '-'}",
                "title": row["ticker"],
                "meta": "Closed" if closed else "Open",
                "date": row_date.isoformat() if row_date else (row["sell_date"] if closed and row["sell_date"] else row["buy_date"]),
                "value": pl_value,
            }
        )
    return activity


def recent_option_activity(strategies, limit=5):
    rows = sorted(
        strategies,
        key=lambda row: row["updated_at"] or row["created_at"] or row["buy_date"] or "",
        reverse=True,
    )
    return [
        {
            "label": f"{row['strategy_type']} #{row['bet_no'] or row['strategy_no']}",
            "title": row["underlying"],
            "meta": row["status"],
            "date": row["sell_date"] or row["buy_date"],
            "value": row["strategy_pl"] if (row["status"] or "").lower() == "closed" else None,
        }
        for row in rows[:limit]
    ]


def month_shift(value, months):
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def option_label(row):
    bet_no = row.get("bet_no") or row.get("strategy_no") or "-"
    return f"{row.get('strategy_type') or 'Option'} #{bet_no} - {row.get('underlying') or '-'}"


def build_pl_transactions(positions, strategies, source_filter="all"):
    transactions = []

    if source_filter in {"all", "stock"}:
        normalized_dates = normalized_buy_date_map(positions)
        for row in positions:
            if not is_closed_position(row) or row["sell_price"] is None:
                continue
            sold_at = parse_shared_date(row["sell_date"])
            if not sold_at:
                continue
            profit = realized_pl(row)
            buy_date = normalized_dates.get(position_identity(row)) or parse_shared_date(row["buy_date"])
            transactions.append(
                {
                    "date": sold_at,
                    "date_label": sold_at.strftime("%d %b %Y"),
                    "source": "Stock",
                    "label": f"Bet {row['bet_no'] or '-'} - {row['ticker']}",
                    "ticker": row["ticker"],
                    "pl": profit or 0,
                    "return_pct": realized_pl_pct(row),
                    "capital": (row["buy_price"] or 0) * (row["qty"] or 0),
                    "held_days": (sold_at - buy_date).days if buy_date else None,
                    "status": "Win" if (profit or 0) > 0 else "Loss" if (profit or 0) < 0 else "Flat",
                }
            )

    if source_filter in {"all", "option"}:
        for row in strategies:
            if (row["status"] or "").lower() != "closed" or row["strategy_pl"] is None:
                continue
            sold_at = parse_shared_date(row["sell_date"])
            if not sold_at:
                continue
            buy_date = parse_shared_date(row["buy_date"])
            profit = row["strategy_pl"] or 0
            transactions.append(
                {
                    "date": sold_at,
                    "date_label": sold_at.strftime("%d %b %Y"),
                    "source": "Option",
                    "label": option_label(row),
                    "ticker": row["underlying"],
                    "pl": profit,
                    "return_pct": row.get("display_pl_pct"),
                    "capital": row.get("total_trade_capital") or row.get("margin") or 0,
                    "held_days": (sold_at - buy_date).days if buy_date else None,
                    "status": "Win" if profit > 0 else "Loss" if profit < 0 else "Flat",
                }
            )

    return sorted(transactions, key=lambda item: (item["date"], item["source"], item["label"]), reverse=True)


def summarize_pl_items(items):
    total = sum(item["pl"] for item in items)
    wins = [item for item in items if item["pl"] > 0]
    losses = [item for item in items if item["pl"] < 0]
    capital = sum(item.get("capital") or 0 for item in items)
    return {
        "count": len(items),
        "wins": len(wins),
        "losses": len(losses),
        "profit": total,
        "win_rate": (len(wins) / len(items) * 100) if items else None,
        "avg_pl": (total / len(items)) if items else None,
        "return_pct": (total / capital * 100) if capital else None,
    }


def rollup_transactions(transactions, key_func, label_func):
    grouped = defaultdict(list)
    for item in transactions:
        grouped[key_func(item["date"])].append(item)

    rows = []
    for key in sorted(grouped.keys(), reverse=True):
        items = grouped[key]
        rows.append(
            {
                "key": key,
                "label": label_func(key),
                "items": sorted(items, key=lambda item: (item["date"], item["label"]), reverse=True),
                **summarize_pl_items(items),
            }
        )
    return rows


def build_month_calendar(transactions, selected_year, selected_month):
    by_day = defaultdict(list)
    month_items = []
    for item in transactions:
        if item["date"].year == selected_year and item["date"].month == selected_month:
            by_day[item["date"].day].append(item)
            month_items.append(item)

    first_weekday, days_in_month = calendar.monthrange(selected_year, selected_month)
    cells = []
    for _ in range(first_weekday):
        cells.append({"empty": True})

    for day in range(1, days_in_month + 1):
        items = by_day.get(day, [])
        cells.append(
            {
                "empty": False,
                "date": date(selected_year, selected_month, day),
                "day": day,
                "items": sorted(items, key=lambda item: item["label"]),
                **summarize_pl_items(items),
            }
        )

    while len(cells) % 7:
        cells.append({"empty": True})

    return {
        "month_name": calendar.month_name[selected_month],
        "month_short": calendar.month_abbr[selected_month],
        "year": selected_year,
        "month": selected_month,
        "month_start": date(selected_year, selected_month, 1),
        **summarize_pl_items(month_items),
        "weeks": [cells[index:index + 7] for index in range(0, len(cells), 7)],
    }


def months_between(start_date, end_date):
    current = date(start_date.year, start_date.month, 1)
    final = date(end_date.year, end_date.month, 1)
    months = []
    while current <= final:
        months.append(current)
        current = month_shift(current, 1)
    return months


def build_pl_calendar(source_filter="all", year=None, month=None, start_date=None, end_date=None):
    positions = list_positions()
    strategies = list_option_strategies()
    for row in strategies:
        row["display_pl_pct"] = option_strategy_return_pct(row)
    all_transactions = build_pl_transactions(positions, strategies, source_filter=source_filter)

    latest_date = all_transactions[0]["date"] if all_transactions else datetime.now().date()
    end_date = end_date or latest_date
    start_date = start_date or month_shift(end_date, -2).replace(day=1)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    transactions = [
        item for item in all_transactions
        if start_date <= item["date"] <= end_date
    ]
    selected_year = int(year or latest_date.year)
    selected_month = int(month or latest_date.month)
    current_month_start = date(selected_year, selected_month, 1)

    daily = rollup_transactions(
        transactions,
        key_func=lambda value: value,
        label_func=lambda key: key.strftime("%d %b %Y"),
    )
    weekly = rollup_transactions(
        transactions,
        key_func=lambda value: value - timedelta(days=value.weekday()),
        label_func=lambda key: f"{key.strftime('%d %b %Y')} - {(key + timedelta(days=6)).strftime('%d %b %Y')}",
    )
    monthly = rollup_transactions(
        transactions,
        key_func=lambda value: date(value.year, value.month, 1),
        label_func=lambda key: key.strftime("%b %Y"),
    )
    yearly = rollup_transactions(
        transactions,
        key_func=lambda value: date(value.year, 1, 1),
        label_func=lambda key: str(key.year),
    )
    available_months = sorted(
        {date(item["date"].year, item["date"].month, 1) for item in all_transactions},
        reverse=True,
    ) or [current_month_start]
    calendar_months = [
        build_month_calendar(transactions, item.year, item.month)
        for item in months_between(start_date, end_date)
    ]

    return {
        "source_filter": source_filter,
        "transactions": transactions,
        "summary": summarize_pl_items(transactions),
        "calendar": build_month_calendar(transactions, selected_year, selected_month),
        "calendar_months": calendar_months,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
        "available_months": available_months,
        "previous_month": month_shift(current_month_start, -1),
        "next_month": month_shift(current_month_start, 1),
        "start_date": start_date,
        "end_date": end_date,
        "date_range_label": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
    }


def build_home_dashboard():
    positions = list_positions()
    strategies = list_option_strategies()
    for row in strategies:
        row["display_pl_pct"] = option_strategy_return_pct(row)
    stock_metrics = stock_dashboard_metrics(positions)
    option_metrics = option_dashboard_metrics(strategies)
    open_bets = [row for row in positions if not is_closed_position(row)]
    try:
        review_summary = build_dashboard_review_summary(open_bets, positions)
    except Exception:
        app.logger.exception("Could not build portfolio review snapshot for dashboard")
        review_summary = {
            "mandatory_sell_review_count": 0,
            "deep_drawdown_count": 0,
            "above_avg_hold_count": 0,
            "five_year_hold_count": 0,
            "degraded_membership_count": 0,
            "promoted_membership_count": 0,
        }

    combined_realized = (stock_metrics["realized_pl"] or 0) + (option_metrics["realized_pl"] or 0)
    return {
        "stock": stock_metrics,
        "options": option_metrics,
        "review": review_summary,
        "combined_realized": combined_realized,
        "stock_activity": recent_stock_activity(positions),
        "option_activity": recent_option_activity(strategies),
    }


def db_row_to_v1_position(row):
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
        status="Closed" if is_closed_position(row) else "Open",
    )


def log_ticker_snapshot(rows, show_target_price):
    app.logger.info(
        "Report data snapshot: total_positions=%s show_target_price=%s",
        len(rows),
        show_target_price,
    )

    for row in rows:
        current_price = row["current_price"]
        target_price = row["target_price"]
        stop_loss = row["stop_loss"]
        buy_price = row["buy_price"]
        qty = row["qty"]
        status = "Closed" if is_closed_position(row) else "Open"

        app.logger.debug(
            "Ticker detail: bet_no=%s ticker=%s status=%s qty=%s buy=%s current=%s target=%s stop_loss=%s sell=%s",
            row["bet_no"],
            row["ticker"],
            status,
            qty,
            buy_price,
            current_price,
            target_price,
            stop_loss,
            row["sell_price"],
        )

        if status != "Open" or current_price is None:
            continue

        if target_price and current_price >= target_price:
            profit = (current_price - buy_price) * qty
            profit_percent = ((current_price - buy_price) / buy_price) * 100 if buy_price else 0
            app.logger.info(
                "Alert preview TARGET HIT: bet_no=%s ticker=%s current=%s target=%s profit=%s profit_percent=%.2f",
                row["bet_no"],
                row["ticker"],
                current_price,
                target_price,
                profit,
                profit_percent,
            )
            app.logger.info(
                "WhatsApp message preview:\nTARGET HIT\nBet       : %s\nStock     : %s\nQty       : %s\nBuy Price : %.2f\nCurrent   : %.2f\nTarget    : %.2f\nP/L       : %.2f\nP/L %%     : %.2f%%",
                row["bet_no"],
                row["ticker"],
                qty,
                buy_price,
                current_price,
                target_price,
                profit,
                profit_percent,
            )
        elif stop_loss and current_price <= stop_loss:
            profit = (current_price - buy_price) * qty
            profit_percent = ((current_price - buy_price) / buy_price) * 100 if buy_price else 0
            app.logger.info(
                "Alert preview STOP LOSS HIT: bet_no=%s ticker=%s current=%s stop_loss=%s profit=%s profit_percent=%.2f",
                row["bet_no"],
                row["ticker"],
                current_price,
                stop_loss,
                profit,
                profit_percent,
            )
            app.logger.info(
                "WhatsApp message preview:\nSTOP LOSS HIT\nBet       : %s\nStock     : %s\nQty       : %s\nBuy Price : %.2f\nCurrent   : %.2f\nStop Loss : %.2f\nP/L       : %.2f\nP/L %%     : %.2f%%",
                row["bet_no"],
                row["ticker"],
                qty,
                buy_price,
                current_price,
                stop_loss,
                profit,
                profit_percent,
            )
        elif target_price:
            remaining = target_price - current_price
            remaining_percent = (remaining / target_price) * 100 if target_price else 0
            if 0 <= remaining_percent <= 5:
                app.logger.info(
                    "Alert preview NEAR TARGET: bet_no=%s ticker=%s current=%s target=%s remaining=%s distance_percent=%.2f",
                    row["bet_no"],
                    row["ticker"],
                    current_price,
                    target_price,
                    remaining,
                    remaining_percent,
                )
                app.logger.info(
                    "WhatsApp message preview:\nNEAR TARGET\nBet       : %s\nStock     : %s\nQty       : %s\nBuy Price : %.2f\nCurrent   : %.2f\nTarget    : %.2f\nRemaining : %.2f\nDistance  : %.2f%%",
                    row["bet_no"],
                    row["ticker"],
                    qty,
                    buy_price,
                    current_price,
                    target_price,
                    remaining,
                    remaining_percent,
                )


def latest_report_snapshot(show_target_price=False, include_app_nav=False):
    report_kind = (
        "app_targets"
        if include_app_nav and show_target_price
        else "app_masked"
        if include_app_nav
        else "targets"
        if show_target_price
        else "masked"
    )
    candidate = latest_report_path(report_kind, REPORTS_DIR)
    return candidate


def add_shared_navigation(report_html, include_app_nav=False):
    """Insert the one shared authenticated navigation into generated reports."""
    if not include_app_nav:
        return report_html
    # Snapshots made by older scheduler processes contain their own report menu.
    # Remove that legacy header before adding the single shared application menu.
    report_html = re.sub(
        r'<header class="app-header">.*?</header>\s*',
        "",
        report_html,
        count=1,
        flags=re.DOTALL,
    )
    return report_html.replace("<body>", f"<body>\n{render_template('_navigation.html')}", 1)


def render_v1_report_from_db(show_target_price=False, include_app_nav=False, refresh_prices=False):
    if refresh_prices:
        refresh_report_prices(force=True)
    rows = list_positions()
    log_ticker_snapshot(rows, show_target_price)
    app.logger.debug(
        "Rendering report from DB: rows=%s show_target_price=%s",
        len(rows),
        show_target_price,
    )
    positions = [db_row_to_v1_position(row) for row in rows]
    report_path = ReportGenerator().generate_html(
        positions,
        show_target_price=show_target_price,
        include_app_nav=include_app_nav,
    )
    app.logger.info("Report generated: path=%s show_target_price=%s", report_path, show_target_price)
    html = Path(report_path).read_text(encoding="utf-8")
    app.logger.debug("Report HTML loaded: bytes=%s", len(html.encode("utf-8")))
    return add_shared_navigation(html, include_app_nav)


def refresh_all_report_snapshots():
    refresh_report_prices(force=True)
    rows = list_positions()
    log_ticker_snapshot(rows, True)
    positions = [db_row_to_v1_position(row) for row in rows]
    generator = ReportGenerator()
    paths = {
        "masked": generator.generate_html(positions, show_target_price=False),
        "targets": generator.generate_html(positions, show_target_price=True),
        "app_targets": generator.generate_html(positions, show_target_price=True, include_app_nav=True),
    }
    update_report_manifest(paths)
    cleanup_old_reports(REPORTS_DIR, keep_days=REPORT_KEEP_DAYS)
    app.logger.info("All report snapshots refreshed: %s", paths)
    return paths


def render_v1_report_snapshot(show_target_price=False, include_app_nav=False):
    if is_authenticated() and not is_admin():
        return render_v1_report_from_db(show_target_price, include_app_nav)
    force_refresh = request.args.get("refresh") == "1" if request else False
    if force_refresh:
        app.logger.info(
            "Report refresh requested: show_target_price=%s include_app_nav=%s",
            show_target_price,
            include_app_nav,
        )
        paths = refresh_all_report_snapshots()
        snapshot_key = (
            "app_targets"
            if include_app_nav and show_target_price
            else "app_masked"
            if include_app_nav
            else "targets"
            if show_target_price
            else "masked"
        )
        return add_shared_navigation(
            Path(paths[snapshot_key]).read_text(encoding="utf-8"),
            include_app_nav,
        )

    snapshot_path = latest_report_snapshot(
        show_target_price=show_target_price,
        include_app_nav=include_app_nav,
    )
    if snapshot_path:
        app.logger.info(
            "Serving cached report snapshot: path=%s show_target_price=%s include_app_nav=%s",
            snapshot_path,
            show_target_price,
            include_app_nav,
        )
        return add_shared_navigation(
            snapshot_path.read_text(encoding="utf-8"),
            include_app_nav,
        )

    app.logger.info(
        "No cached report snapshot found; generating once: show_target_price=%s include_app_nav=%s",
        show_target_price,
        include_app_nav,
    )
    return render_v1_report_from_db(
        show_target_price=show_target_price,
        include_app_nav=include_app_nav,
        refresh_prices=False,
    )


def row_price_is_stale(row, max_age_minutes):
    if row["current_price"] is None:
        return True
    if max_age_minutes <= 0:
        return False
    try:
        updated_at = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        updated_date = parse_shared_date(row["updated_at"])
        if updated_date is None:
            return True
        updated_at = datetime.combine(updated_date, datetime.min.time())
    return datetime.now() - updated_at >= timedelta(minutes=max_age_minutes)


def refresh_report_prices(force=False):
    rows = list_positions()
    open_rows = [row for row in rows if not is_closed_position(row)]
    stale_rows = [
        row for row in open_rows
        if force or row_price_is_stale(row, REPORT_PRICE_REFRESH_MAX_AGE_MINUTES)
    ]
    updated = 0
    failed = 0
    skipped = len(open_rows) - len(stale_rows)
    app.logger.info(
        "Report page price refresh started: open_bets=%s stale=%s skipped=%s max_age_minutes=%s force=%s",
        len(open_rows),
        len(stale_rows),
        skipped,
        REPORT_PRICE_REFRESH_MAX_AGE_MINUTES,
        force,
    )
    for row in stale_rows:
        ticker = row["ticker"]
        try:
            price = latest_price(ticker)
            update_current_price(row["id"], price)
            updated += 1
            app.logger.info(
                "Report page price updated: bet_no=%s ticker=%s old_price=%s new_price=%s",
                row["bet_no"],
                ticker,
                row["current_price"],
                price,
            )
        except Exception:
            failed += 1
            app.logger.warning("Report page price refresh failed: bet_no=%s ticker=%s", row["bet_no"], ticker)
    app.logger.info("Report page price refresh completed: updated=%s failed=%s skipped=%s", updated, failed, skipped)


def parse_float(value, required=False, field_name="Number"):
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid number") from exc
    return parsed


def validate_positive(value, field_name, allow_zero=False):
    if value is None:
        return
    if allow_zero and value == 0:
        return
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")


def validate_price(value, field_name):
    validate_positive(value, field_name)
    if value is not None and value > MAX_PRICE_VALUE:
        raise ValueError(f"{field_name} looks too high. Please check the entered value.")


def parse_int(value):
    if value is None or str(value).strip() == "":
        return None
    return int(value)


TICKER_PATTERN = re.compile(r"[A-Z0-9&._\- ]{1,30}")


def parse_form_date(value, field_name, required=False):
    text = (value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date (YYYY-MM-DD).") from exc


def position_from_form(form):
    ticker = (form.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("Ticker is required.")
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError("Ticker may only contain letters, numbers, spaces and & . - _ (max 30).")
    buy_date = parse_form_date(form.get("buy_date"), "Buy Date", required=True)
    parsed_sell_date = parse_form_date(form.get("sell_date"), "Sell Date")
    if parsed_sell_date and parsed_sell_date < buy_date:
        raise ValueError("Sell Date cannot be before Buy Date.")
    trade_group_id = parse_int(form.get("trade_group_id"))
    sell_price = parse_float(form.get("sell_price"), field_name="Sell Price")
    current_price = parse_float(form.get("current_price"), field_name="Current Price")
    qty = parse_float(form.get("qty"), required=True, field_name="Quantity")
    buy_price = parse_float(form.get("buy_price"), required=True, field_name="Buy Price")
    target_price = parse_float(form.get("target_price"), required=True, field_name="Target Price")
    stop_loss = parse_float(form.get("stop_loss"), field_name="Stop Loss")
    sell_date = parsed_sell_date.isoformat() if parsed_sell_date else None
    status = "Closed" if form.get("status") == "Closed" else "Open"

    validate_positive(qty, "Quantity")
    validate_price(buy_price, "Buy Price")
    validate_price(current_price, "Current Price")
    validate_price(target_price, "Target Price")
    validate_price(stop_loss, "Stop Loss")
    validate_price(sell_price, "Sell Price")

    if sell_date and sell_price is None:
        raise ValueError("Sell Price is required when Sell Date is entered.")
    if sell_price is not None and not sell_date:
        raise ValueError("Sell Date is required when Sell Price is entered.")
    if status == "Closed" and (not sell_date or sell_price is None):
        raise ValueError("Closed bets require both Sell Date and Sell Price.")
    if trade_group_id and not get_stock_trade_group(trade_group_id):
        raise ValueError("Stock Trade Group is required.")

    data = {
        "bet_no": parse_int(form.get("bet_no")),
        "trade_group_id": trade_group_id,
        "ticker": ticker,
        "buy_date": buy_date.isoformat(),
        "qty": qty,
        "buy_price": buy_price,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "current_price": current_price,
        "sell_date": sell_date,
        "sell_price": sell_price,
        "status": status,
        "notes": None,
        "is_test_data": False,
    }
    app.logger.debug("Parsed position form data: %s", data)
    return data


def parse_optional_float(form, key, field_name):
    return parse_float(form.get(key), field_name=field_name)


def option_strategy_from_form(form):
    status = "Closed" if form.get("status") == "Closed" else "Open"
    sell_date = form.get("sell_date") or None
    if status == "Closed" and not sell_date:
        raise ValueError("Closed option strategies require Sell Date.")

    data = {
        "strategy_no": parse_int(form.get("strategy_no")),
        "bet_no": parse_int(form.get("bet_no")),
        "trade_group_id": parse_int(form.get("trade_group_id")),
        "strategy_type": form.get("strategy_type") or "Custom Multi-leg",
        "underlying": (form.get("underlying") or "").strip().upper(),
        "buy_date": form.get("buy_date"),
        "sell_date": sell_date,
        "expiry_date": form.get("expiry_date") or None,
        "status": status,
        "margin": None,
        "max_risk": None,
        "max_profit": None,
        "strategy_pl": None,
        "pl_pct": None,
        "stock_move_pct": parse_optional_float(form, "stock_move_pct", "Stock Move %"),
        "long_only_pl": None,
        "notes": form.get("notes") or None,
        "is_test_data": False,
    }

    if not data["underlying"]:
        raise ValueError("Underlying is required.")
    if not data["trade_group_id"] or not get_trade_group(data["trade_group_id"]):
        raise ValueError("Trade Group is required.")
    if not data["buy_date"]:
        raise ValueError("Buy Date is required.")

    if sell_date and data["stock_move_pct"] is None:
        try:
            data["stock_move_pct"] = historical_stock_move_pct(data["underlying"], data["buy_date"], sell_date)
        except Exception:
            app.logger.exception(
                "Could not calculate stock move: underlying=%s buy_date=%s sell_date=%s",
                data["underlying"],
                data["buy_date"],
                sell_date,
            )

    actions = form.getlist("leg_action[]")
    tickers = form.getlist("leg_ticker[]")
    qty_values = form.getlist("leg_qty[]")
    buy_prices = form.getlist("leg_buy_price[]")
    sell_prices = form.getlist("leg_sell_price[]")

    legs = []
    for index, ticker in enumerate(tickers):
        ticker = (ticker or "").strip()
        if not ticker:
            continue
        qty = parse_float(qty_values[index] if index < len(qty_values) else None, required=True, field_name=f"Leg {index + 1} Quantity")
        buy_price = parse_float(buy_prices[index] if index < len(buy_prices) else None, required=True, field_name=f"Leg {index + 1} Entry Premium")
        sell_price = parse_float(sell_prices[index] if index < len(sell_prices) else None, field_name=f"Leg {index + 1} Exit Premium")
        validate_positive(qty, f"Leg {index + 1} Quantity")
        validate_price(buy_price, f"Leg {index + 1} Entry Premium")
        validate_price(sell_price, f"Leg {index + 1} Exit Premium")
        if status == "Closed" and sell_price is None:
            raise ValueError(f"Leg {index + 1} Exit Premium is required for closed strategies.")

        legs.append(
            {
                "action": actions[index] if index < len(actions) else "BUY",
                "ticker": ticker,
                "qty": qty,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "status": status,
            }
        )

    if not legs:
        raise ValueError("At least one option leg is required.")

    return data, legs


def option_legs_from_request(form):
    actions = form.getlist("leg_action[]")
    tickers = form.getlist("leg_ticker[]")
    qty_values = form.getlist("leg_qty[]")
    buy_prices = form.getlist("leg_buy_price[]")
    sell_prices = form.getlist("leg_sell_price[]")
    rows = []
    for index, ticker in enumerate(tickers):
        if not (ticker or "").strip():
            continue
        rows.append(
            {
                "leg_no": index + 1,
                "action": actions[index] if index < len(actions) else "BUY",
                "ticker": ticker,
                "qty": qty_values[index] if index < len(qty_values) else "",
                "buy_price": buy_prices[index] if index < len(buy_prices) else "",
                "sell_price": sell_prices[index] if index < len(sell_prices) else "",
            }
        )
    return rows or [
        {
            "leg_no": 1,
            "action": "BUY",
            "ticker": "",
            "qty": "",
            "buy_price": "",
            "sell_price": "",
        }
    ]


def form_values_from_request(form):
    fields = [
        "bet_no", "trade_group_id", "ticker", "buy_date", "qty", "buy_price", "current_price",
        "target_price", "stop_loss", "sell_date", "sell_price", "status", "notes",
    ]
    return SimpleNamespace(**{field: form.get(field, "") for field in fields})


def stock_alert_from_form(form):
    ticker = (form.get("ticker") or "").strip().upper()
    trigger_price = parse_float(form.get("trigger_price"), required=True, field_name="Trigger Price")
    direction = (form.get("direction") or "ABOVE").strip().upper()
    channel = (form.get("channel") or "BOTH").strip().upper()
    notes = (form.get("notes") or "").strip()

    if not ticker:
        raise ValueError("Ticker is required.")
    validate_price(trigger_price, "Trigger Price")
    if direction not in STOCK_ALERT_DIRECTIONS:
        raise ValueError("Alert direction must be Above or Below.")
    if channel not in STOCK_ALERT_CHANNELS:
        raise ValueError("Alert channel must be WhatsApp, Telegram, or Both.")

    return {
        "ticker": ticker,
        "trigger_price": trigger_price,
        "direction": direction,
        "channel": channel,
        "notes": notes or None,
        "is_active": form.get("is_active", "on") == "on",
        "reset_triggered": form.get("reset_triggered", "on") == "on",
    }


def stock_alert_form_values(form):
    return SimpleNamespace(
        ticker=(form.get("ticker") or "").strip().upper(),
        trigger_price=form.get("trigger_price") or "",
        direction=(form.get("direction") or "ABOVE").strip().upper(),
        channel=(form.get("channel") or "BOTH").strip().upper(),
        notes=form.get("notes") or "",
        is_active=form.get("is_active", "on") == "on",
    )


def refresh_price_after_save(position_id, ticker):
    try:
        price = latest_price(ticker)
        update_current_price(position_id, price)
        app.logger.info("Price auto-updated after save: id=%s ticker=%s price=%s", position_id, ticker, price)
        return price, None
    except Exception as exc:
        app.logger.exception("Could not auto-update price after save: id=%s ticker=%s", position_id, ticker)
        return None, exc


@app.before_request
def log_request_start():
    app.logger.info("Request started: %s %s", request.method, request.path)
    app.logger.debug("Request args=%s form_keys=%s", dict(request.args), list(request.form.keys()))
    if request.method == "POST":
        validate_csrf()


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.get("/health")
def health():
    from db import connect

    db_ok = False
    try:
        with connect() as connection:
            connection.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        app.logger.exception("Health check database probe failed")

    heartbeat = read_heartbeat()
    heartbeat_age = heartbeat_age_minutes(heartbeat)
    market_check_age = market_check_age_minutes(heartbeat)
    scheduler_running = scheduler_process_running(heartbeat)
    scheduler_stale = not scheduler_running or is_heartbeat_stale(heartbeat, SCHEDULER_STALE_MINUTES)
    if heartbeat is None:
        scheduler_state = "not_started"
    elif not scheduler_running:
        scheduler_state = "not_running"
    elif scheduler_stale:
        scheduler_state = "stale"
    else:
        scheduler_state = "ok"
    status = "ok" if db_ok and scheduler_state == "ok" else "degraded"
    return jsonify(
        {
            "status": status,
            "app": "StockMonitor-V3",
            "database": "ok" if db_ok else "error",
            "scheduler": {
                "state": scheduler_state,
                "running": scheduler_running,
                "heartbeat_path": str(HEARTBEAT_PATH),
                "heartbeat_at": heartbeat.get("at") if heartbeat else None,
                "age_minutes": heartbeat_age,
                "last_market_check_at": heartbeat.get("last_market_check_at") if heartbeat else None,
                "market_check_age_minutes": market_check_age,
                "stale": scheduler_stale,
                "stale_threshold_minutes": SCHEDULER_STALE_MINUTES,
            },
        }
    )


@app.route("/")
@login_required
def index():
    dashboard = build_home_dashboard()
    return render_template(
        "index.html",
        dashboard=dashboard,
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title="Dashboard",
    )


@app.route("/report")
@login_required
def report_masked():
    return Response(render_v1_report_snapshot(include_app_nav=False), mimetype="text/html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if login_locked(username):
            app.logger.warning("Login locked after repeated failures: username=%s", username)
            flash("Too many failed sign-in attempts. Try again in 15 minutes.", "error")
            return render_template("login.html")
        user = get_user_by_username(username)
        # The bootstrap password (ADMIN_PASSWORD_HASH, or the development default)
        # only works until the admin account has a password of its own.
        needs_bootstrap = username == ADMIN_USERNAME and (not user or not user.get("password_hash"))
        valid_bootstrap_admin = needs_bootstrap and check_password_hash(ACTIVE_PASSWORD_HASH, password)
        if user and not user["is_active"]:
            valid_bootstrap_admin = False
        valid_user = user and user["is_active"] and user.get("password_hash") and check_password_hash(user["password_hash"], password)
        uses_default_password = bool(valid_user or valid_bootstrap_admin) and password == DEFAULT_ADMIN_PASSWORD
        if uses_default_password and not IS_LOCAL_BIND:
            app.logger.warning("Login refused: default password on hosted server: username=%s", username)
            flash("This account still uses the default password. Set a new one with reset_password.py on the server.", "error")
            return render_template("login.html")
        if valid_user or valid_bootstrap_admin:
            if not user:
                flash("Account setup is incomplete. Please restart the application once.", "error")
                return render_template("login.html")
            if valid_bootstrap_admin and not user.get("password_hash"):
                set_user_password(user["id"], ACTIVE_PASSWORD_HASH)
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            if uses_default_password:
                session["must_change_password"] = True
                flash("You are using the default password. Change it now from Account > Change password.", "error")
            csrf_token()
            clear_login_failures(username)
            app.logger.info("Login succeeded: username=%s", username)
            return redirect(safe_next_url(request.args.get("next")) or url_for("report_targets"))
        record_login_failure(username)
        app.logger.warning("Login failed: username=%s", username)
        flash("Invalid username or password.")

    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


@app.route("/users", methods=["GET", "POST"])
@admin_required
def manage_users():
    if request.method == "POST":
        validate_csrf()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        whatsapp_number = (request.form.get("whatsapp_number") or "").strip()
        telegram_chat_id = (request.form.get("telegram_chat_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            flash("Username must be 3–40 characters: letters, numbers, dot, dash, or underscore.", "error")
        elif len(password) < 10:
            flash("Password must contain at least 10 characters.", "error")
        elif not email or "@" not in email:
            flash("A valid email address is required.", "error")
        elif get_user_by_username(username):
            flash("That username already exists.", "error")
        else:
            user_id = create_user(username, generate_password_hash(password), full_name, email, whatsapp_number, telegram_chat_id)
            token = set_current_user_id(user_id)
            try:
                create_stock_trade_group(f"Swing Trading ({username})", f"SWG{user_id}", "Default stock group")
                create_trade_group(f"Options ({username})", f"OPT{user_id}", "Default options group")
            finally:
                reset_current_user_id(token)
            flash(f"User {username} created. They can now sign in.", "success")
            return redirect(url_for("manage_users"))
    return render_template("users.html", users=list_users(), title="User Management")


@app.post("/users/<int:user_id>/profile")
@admin_required
def update_user_profile_route(user_id):
    validate_csrf()
    target = next((user for user in list_users() if user["id"] == user_id), None)
    if not target:
        abort(404)
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("A valid email address is required.", "error")
    else:
        update_user_profile(
            user_id,
            (request.form.get("full_name") or "").strip(),
            email,
            (request.form.get("whatsapp_number") or "").strip(),
            (request.form.get("telegram_chat_id") or "").strip(),
        )
        flash(f"Contact details for {target['username']} updated.", "success")
    return redirect(url_for("manage_users"))


@app.post("/account/change-password")
@login_required
def change_password():
    validate_csrf()
    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    user = get_user_by_username(session.get("username") or "")
    valid_current = user and user.get("password_hash") and check_password_hash(user["password_hash"], current_password)
    if not valid_current and session.get("username") == ADMIN_USERNAME and user and not user.get("password_hash"):
        valid_current = check_password_hash(ACTIVE_PASSWORD_HASH, current_password)
    if not valid_current:
        flash("Current password is incorrect.", "error")
    elif len(new_password) < 10:
        flash("New password must contain at least 10 characters.", "error")
    else:
        set_user_password(session["user_id"], generate_password_hash(new_password))
        session.pop("must_change_password", None)
        flash("Password changed successfully.", "success")
    return redirect(request.referrer or url_for("index"))


@app.post("/users/<int:user_id>/toggle")
@admin_required
def toggle_user(user_id):
    validate_csrf()
    if user_id == session.get("user_id"):
        flash("You cannot disable your own account.", "error")
    else:
        target = next((user for user in list_users() if user["id"] == user_id), None)
        if not target:
            flash("User not found.", "error")
        elif target["is_admin"]:
            flash("Admin accounts cannot be disabled here.", "error")
        else:
            set_user_active(user_id, not target["is_active"])
            flash(f"User {target['username']} {'enabled' if not target['is_active'] else 'disabled'}.", "success")
    return redirect(url_for("manage_users"))


@app.route("/report-targets")
@login_required
def report_targets():
    return Response(render_v1_report_snapshot(show_target_price=True, include_app_nav=True), mimetype="text/html")


@app.route("/portfolio-analytics")
@login_required
def portfolio_analytics():
    positions = list_positions(trade_group_code=None, reportable_only=True)
    strategies = list_option_strategies(reportable_only=True)
    analytics = build_portfolio_analytics(
        positions,
        strategies,
        option_strategy_risk_capital,
        performance_snapshots=list_portfolio_performance_snapshots(),
    )
    return render_template(
        "portfolio_analytics.html",
        analytics=analytics,
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title="Portfolio Analytics",
    )


@app.route("/pl-calendar")
@login_required
def pl_calendar():
    source_filter = (request.args.get("source") or "all").strip().lower()
    if source_filter not in {"all", "stock", "option"}:
        source_filter = "all"

    try:
        selected_year = int(request.args.get("year") or 0) or None
        selected_month = int(request.args.get("month") or 0) or None
        if selected_month is not None and not 1 <= selected_month <= 12:
            selected_month = None
    except ValueError:
        selected_year = None
        selected_month = None

    today = datetime.now().date()
    start_date = parse_shared_date(request.args.get("start"))
    end_date = parse_shared_date(request.args.get("end"))
    preset = (request.args.get("preset") or "").strip().lower()
    if preset == "7d":
        end_date = today
        start_date = today - timedelta(days=6)
    elif preset == "30d":
        end_date = today
        start_date = today - timedelta(days=29)
    elif preset == "3m":
        end_date = today
        start_date = month_shift(today, -2).replace(day=1)
    elif preset == "6m":
        end_date = today
        start_date = month_shift(today, -5).replace(day=1)
    elif preset == "fy":
        fy_start_year = today.year if today.month >= 4 else today.year - 1
        start_date = date(fy_start_year, 4, 1)
        end_date = date(fy_start_year + 1, 3, 31)

    report = build_pl_calendar(
        source_filter=source_filter,
        year=selected_year,
        month=selected_month,
        start_date=start_date,
        end_date=end_date,
    )
    app.logger.info(
        "P/L calendar loaded: source=%s transactions=%s month=%s-%s",
        source_filter,
        len(report["transactions"]),
        report["calendar"]["year"],
        report["calendar"]["month"],
    )
    presets = [
        ("7d", "Last 7 Days"),
        ("30d", "Last 30 Days"),
        ("3m", "Last 3 months"),
        ("6m", "Last 6 months"),
        ("fy", "F.Y."),
    ]
    return render_template(
        "pl_calendar.html",
        report=report,
        presets=presets,
        money=money,
        pct=pct,
        pl_class=pl_class,
        title="P/L Calendar",
    )


@app.get("/api/portfolio/actual-pl")
def api_portfolio_actual_pl():
    unauthorized = require_portfolio_api_key()
    if unauthorized:
        return unauthorized

    api_user = get_user_by_username(API_USERNAME)
    if not api_user:
        app.logger.error("Portfolio API user not found: %s", API_USERNAME)
        return jsonify({"error": "API user not configured"}), 500
    user_token = set_current_user_id(api_user["id"])
    try:
        return jsonify(build_portfolio_api_payload())
    finally:
        reset_current_user_id(user_token)


def build_portfolio_api_payload():
    include_details = request.args.get("details", "1").strip().lower() not in {"0", "false", "no"}
    stock_positions = list_positions(trade_group_code=None, reportable_only=True)
    option_strategies = list_option_strategies(reportable_only=True)
    stock_payload = stock_actual_pl_payload(stock_positions)
    option_payload = option_actual_pl_payload(option_strategies)
    year_wise = realized_pl_by_year(stock_positions, option_strategies)

    payload = {
        "as_of": datetime.now().replace(microsecond=0).isoformat(),
        "currency": "INR",
        "source": "StockMonitor-V3",
        "combined": {
            "realized_pl": api_float(
                (stock_payload["summary"]["realized_pl"] or 0)
                + (option_payload["summary"]["realized_pl"] or 0)
            ),
            "actual_pl": api_float(
                (stock_payload["summary"]["actual_pl"] or 0)
                + (option_payload["summary"]["actual_pl"] or 0)
            ),
            "stock_realized_pl": stock_payload["summary"]["realized_pl"],
            "stock_unrealized_pl": stock_payload["summary"]["unrealized_pl"],
            "option_realized_pl": option_payload["summary"]["realized_pl"],
        },
        "year_wise": year_wise,
        "stocks": stock_payload,
        "options": option_payload,
    }
    if not include_details:
        payload["stocks"].pop("bets", None)
        payload["options"].pop("bets", None)
    return payload


@app.route("/stock-profit-booking-simulation")
@login_required
def stock_profit_booking_simulation():
    positions = list_positions()
    simulation = simulate_stock_profit_booking(positions)
    record_stock_stop_recovery_audit(simulation["rows"])
    record_stock_booking_simulation_snapshot(simulation["summary"])
    snapshots = list_stock_booking_simulation_snapshots(limit=12)
    recommendation_counts = Counter(
        snapshot["recommended_rate"]
        for snapshot in snapshots
        if snapshot.get("recommended_rate")
    )
    recommendation_stability = {
        "total": sum(recommendation_counts.values()),
        "targets": [
            {"label": label, "count": count}
            for label, count in sorted(
                recommendation_counts.items(),
                key=lambda item: (-item[1], float(item[0].rstrip("%"))),
            )
        ],
    }
    recommendation_stability["leading"] = (
        recommendation_stability["targets"][0]
        if recommendation_stability["targets"]
        else None
    )
    return render_template(
        "stock_profit_booking_simulation.html",
        rows=simulation["rows"],
        summary=simulation["summary"],
        snapshots=snapshots,
        recommendation_stability=recommendation_stability,
        target_rates=simulation["target_rates"],
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        display_date=display_shared_date,
        title="Stock Profit Booking Simulation",
    )


@app.route("/historical-portfolio-simulation")
@login_required
def historical_portfolio_simulation():
    target_param = (request.args.get("target") or "7").strip().lower()
    if target_param == "actual":
        detail_target = "actual"
    elif target_param in {"none", "no-target"}:
        detail_target = None
    else:
        try:
            detail_target = float(target_param.rstrip("%")) / 100
        except ValueError:
            detail_target = 0.07
    allowed_targets = {None, "actual", 0.06, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.26}
    if detail_target not in allowed_targets:
        detail_target = 0.07
    simulation = simulate_historical_portfolio(list_positions(), detail_target=detail_target)
    return render_template(
        "historical_portfolio_simulation.html",
        simulation=simulation,
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title="Historical Portfolio Simulator",
    )


@app.route("/funding-sustainability-simulation")
@login_required
def funding_sustainability_simulation():
    target_param = (request.args.get("target") or "7").strip().lower()
    if target_param in {"none", "no-target"}:
        detail_target = None
    else:
        try:
            detail_target = float(target_param.rstrip("%")) / 100
        except ValueError:
            detail_target = 0.07
    allowed_targets = {None, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.26}
    if detail_target not in allowed_targets:
        detail_target = 0.07
    simulation = simulate_funding_sustainability(list_positions(), detail_target=detail_target)
    return render_template(
        "funding_sustainability_simulation.html",
        simulation=simulation,
        money=money,
        compact_money=compact_money,
        number=number,
        title="Capital Recycling / Funding Sustainability",
    )


@app.route("/index-membership")
@login_required
def index_membership():
    swing_positions = list_positions()
    open_bets = [row for row in swing_positions if not is_closed_position(row)]
    report = build_index_membership_report(open_bets, swing_positions)
    app.logger.info(
        "Index membership page loaded: open_bets=%s tracked_indices=%s membership_errors=%s status_errors=%s",
        len(open_bets),
        report["tracked_index_count"],
        len(report["membership_errors"]),
        len(report["status_errors"]),
    )
    return render_template(
        "index_membership.html",
        report=report,
        money=money,
        title="Index Membership",
    )


def option_bet_streaks(strategies):
    closed = [
        row for row in strategies
        if (row["status"] or "").lower() == "closed"
    ]
    closed.sort(key=lambda row: (row["sell_date"] or row["buy_date"] or "", row["id"]))

    longest_winning = 0
    longest_losing = 0
    current_winning = 0
    current_losing = 0
    for row in closed:
        pl_value = row["strategy_pl"]
        if pl_value is not None and pl_value > 0:
            current_winning += 1
            current_losing = 0
            longest_winning = max(longest_winning, current_winning)
        elif pl_value is not None and pl_value < 0:
            current_losing += 1
            current_winning = 0
            longest_losing = max(longest_losing, current_losing)
        else:
            current_winning = 0
            current_losing = 0

    return {
        "longest_winning": longest_winning,
        "longest_losing": longest_losing,
    }


def option_dashboard_metrics(strategies):
    closed = [row for row in strategies if (row["status"] or "").lower() == "closed"]
    open_rows = [row for row in strategies if (row["status"] or "").lower() != "closed"]
    wins = [row for row in closed if (row["strategy_pl"] or 0) > 0]
    losses = [row for row in closed if (row["strategy_pl"] or 0) < 0]
    realized_pl = sum(row["strategy_pl"] or 0 for row in closed)
    return_capital = sum(option_strategy_risk_capital(row) or 0 for row in closed if row["strategy_pl"] is not None)
    streaks = option_bet_streaks(strategies)
    by_strategy = {}
    for row in strategies:
        key = row["strategy_type"] or "Unclassified"
        summary = by_strategy.setdefault(
            key,
            {
                "strategy_type": key,
                "total": 0,
                "open": 0,
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "realized_pl": 0,
                "actual_costs": empty_costs(),
                "long_call_pl": 0,
                "long_call_costs": empty_costs(),
                "long_call_count": 0,
                "long_call_trade_total": 0,
                "long_put_pl": 0,
                "long_put_costs": empty_costs(),
                "long_put_count": 0,
                "long_put_trade_total": 0,
                "long_stock_pl": 0,
                "long_stock_costs": empty_costs(),
                "long_stock_count": 0,
                "long_stock_trade_total": 0,
                "long_call_trades": [],
                "long_put_trades": [],
                "long_stock_trades": [],
            },
        )
        summary["total"] += 1
        if row["long_call_pl"] is not None:
            legs = row.get("legs") or []
            costs = single_leg_simulation_costs(legs, "CE")
            summary["long_call_pl"] += row["long_call_pl"]
            summary["long_call_costs"] = add_costs(summary["long_call_costs"], costs)
            summary["long_call_count"] += 1
            summary["long_call_trade_total"] += 1
            if len(summary["long_call_trades"]) < WHATIF_TRADE_LIMIT:
                summary["long_call_trades"].append(
                    {
                        "bet_no": row["bet_no"] or row["strategy_no"],
                        "underlying": row["underlying"],
                        "date": row["sell_date"] or row["buy_date"],
                        "stock_move_pct": row["stock_move_pct"],
                        "amount_risked": single_leg_amount_risked(legs, "CE"),
                        "pl": row["long_call_pl"],
                        "brokerage": costs.get("brokerage") if costs else None,
                        "charges": costs.get("total") if costs else None,
                        "net": net_after_costs(row["long_call_pl"], costs),
                    }
                )
        if row["long_put_pl"] is not None:
            costs = single_leg_simulation_costs(row.get("legs") or [], "PE")
            summary["long_put_pl"] += row["long_put_pl"]
            summary["long_put_costs"] = add_costs(summary["long_put_costs"], costs)
            summary["long_put_count"] += 1
            summary["long_put_trade_total"] += 1
            if len(summary["long_put_trades"]) < WHATIF_TRADE_LIMIT:
                summary["long_put_trades"].append(
                    {
                        "bet_no": row["bet_no"] or row["strategy_no"],
                        "underlying": row["underlying"],
                        "date": row["sell_date"] or row["buy_date"],
                        "stock_move_pct": row["stock_move_pct"],
                        "pl": row["long_put_pl"],
                        "brokerage": costs.get("brokerage") if costs else None,
                        "charges": costs.get("total") if costs else None,
                        "net": net_after_costs(row["long_put_pl"], costs),
                    }
                )
        if (row["status"] or "").lower() == "closed":
            stock_notional = stock_equivalent_notional(row["strategy_type"], row.get("legs") or [])
            stock_pl = stock_simulation_pl_from_move(row["stock_move_pct"], row["strategy_type"], stock_notional)
            costs = stock_simulation_costs(row["stock_move_pct"], row["strategy_type"], stock_notional)
            if stock_pl is not None:
                summary["long_stock_pl"] += stock_pl
                summary["long_stock_costs"] = add_costs(summary["long_stock_costs"], costs)
                summary["long_stock_count"] += 1
            summary["long_stock_trade_total"] += 1
            if len(summary["long_stock_trades"]) < WHATIF_TRADE_LIMIT:
                summary["long_stock_trades"].append(
                    {
                        "bet_no": row["bet_no"] or row["strategy_no"],
                        "underlying": row["underlying"],
                        "date": row["sell_date"] or row["buy_date"],
                        "stock_move_pct": row["stock_move_pct"],
                        "amount_risked": stock_notional,
                        "pl": stock_pl,
                        "brokerage": costs.get("brokerage") if costs else None,
                        "charges": costs.get("total") if costs else None,
                        "net": net_after_costs(stock_pl, costs),
                        "missing_stock_move": row["stock_move_pct"] is None,
                        "stock_scenario_label": stock_simulation_label(row["strategy_type"]),
                    }
                )
        if (row["status"] or "").lower() == "closed":
            summary["closed"] += 1
            summary["realized_pl"] += row["strategy_pl"] or 0
            summary["actual_costs"] = add_costs(summary["actual_costs"], option_strategy_costs(row.get("legs") or []))
            if (row["strategy_pl"] or 0) > 0:
                summary["wins"] += 1
            elif (row["strategy_pl"] or 0) < 0:
                summary["losses"] += 1
        else:
            summary["open"] += 1
    for summary in by_strategy.values():
        if not summary["long_call_count"]:
            summary["long_call_pl"] = None
            summary["long_call_costs"] = None
        if not summary["long_put_count"]:
            summary["long_put_pl"] = None
            summary["long_put_costs"] = None
        if not summary["long_stock_count"]:
            summary["long_stock_pl"] = None
            summary["long_stock_costs"] = None
        summary["actual_brokerage"] = summary["actual_costs"]["total"]
        summary["long_call_brokerage"] = summary["long_call_costs"]["total"] if summary["long_call_costs"] else None
        summary["long_put_brokerage"] = summary["long_put_costs"]["total"] if summary["long_put_costs"] else None
        summary["long_stock_brokerage"] = summary["long_stock_costs"]["total"] if summary["long_stock_costs"] else None
        summary["actual_net"] = net_after_costs(summary["realized_pl"], summary["actual_costs"])
        summary["long_call_net"] = net_after_costs(summary["long_call_pl"], summary["long_call_costs"])
        summary["long_put_net"] = net_after_costs(summary["long_put_pl"], summary["long_put_costs"])
        summary["long_stock_net"] = net_after_costs(summary["long_stock_pl"], summary["long_stock_costs"])
        summary["stock_scenario_label"] = stock_simulation_label(summary["strategy_type"])
        summary["best"] = best_scenario_label(
            [
                ("Actual", summary["actual_net"]),
                ("Long Call", summary["long_call_net"]),
                ("Long Put", summary["long_put_net"]),
                (summary["stock_scenario_label"], summary["long_stock_net"]),
            ]
        )
    return {
        "total": len(strategies),
        "open": len(open_rows),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "outcome_rate": (len(wins) / len(closed) * 100) if closed else None,
        "realized_pl": realized_pl,
        "avg_return": (realized_pl / return_capital * 100) if return_capital else None,
        "longest_winning_streak": streaks["longest_winning"],
        "longest_losing_streak": streaks["longest_losing"],
        "whatif_trade_limit": WHATIF_TRADE_LIMIT,
        "by_strategy": sorted(by_strategy.values(), key=lambda item: item["total"], reverse=True),
    }


def option_whatif_report_rows(strategies):
    rows = []
    for row in strategies:
        if (row["status"] or "").lower() != "closed":
            continue

        legs = row.get("legs") or []
        comparison = option_bet_comparison(row, legs)
        scenario_map = {scenario["label"]: scenario for scenario in comparison["scenarios"]}
        long_call = scenario_map.get("Long Call Only", {})
        long_put = scenario_map.get("Long Put Only", {})
        stock_label = stock_simulation_label(row["strategy_type"])
        stock = scenario_map.get(stock_label, {})
        stock_side = stock_simulation_side(row["strategy_type"])
        stock_notional = comparison.get("stock_notional")

        rows.append(
            {
                "group_name": row["trade_group_name"],
                "group_code": row["trade_group_code"],
                "strategy": row["strategy_type"],
                "bet_no": row["bet_no"] or row["strategy_no"],
                "bet_details": f"{row['trade_group_code']}-{int(row['bet_no'] or row['strategy_no']):03d} - {row['underlying']}",
                "underlying": row["underlying"],
                "date": row["sell_date"] or row["buy_date"],
                "stock_move_pct": row["stock_move_pct"],
                "actual_risk": option_strategy_risk_capital(row),
                "actual_pl": row["strategy_pl"],
                "actual_net": scenario_map.get("Actual Strategy", {}).get("net"),
                "long_call_risk": single_leg_amount_risked(legs, "CE"),
                "long_call_pl": long_call.get("pl"),
                "long_call_net": long_call.get("net"),
                "long_call_charges": long_call.get("charges"),
                "long_put_risk": single_leg_amount_risked(legs, "PE"),
                "long_put_pl": long_put.get("pl"),
                "long_put_net": long_put.get("net"),
                "long_put_charges": long_put.get("charges"),
                "buy_stock_risk": stock_notional if stock_side != "short" and stock.get("pl") is not None else None,
                "buy_stock_pl": stock.get("pl") if stock_side != "short" else None,
                "buy_stock_net": stock.get("net") if stock_side != "short" else None,
                "buy_stock_charges": stock.get("charges") if stock_side != "short" else None,
                "short_stock_risk": stock_notional if stock_side == "short" and stock.get("pl") is not None else None,
                "short_stock_pl": stock.get("pl") if stock_side == "short" else None,
                "short_stock_net": stock.get("net") if stock_side == "short" else None,
                "short_stock_charges": stock.get("charges") if stock_side == "short" else None,
                "legs": legs,
                "best": comparison["best"],
            }
        )
    return sorted(rows, key=lambda item: (item["date"] or "", item["bet_no"]), reverse=True)


def option_whatif_visible_columns(rows):
    scenario_columns = [
        {
            "key": "long_call",
            "label": "Long Call Only P/L",
            "risk_key": "long_call_risk",
            "pl_key": "long_call_pl",
            "net_key": "long_call_net",
            "charges_key": "long_call_charges",
        },
        {
            "key": "long_put",
            "label": "Long Put Only P/L",
            "risk_key": "long_put_risk",
            "pl_key": "long_put_pl",
            "net_key": "long_put_net",
            "charges_key": "long_put_charges",
        },
        {
            "key": "buy_stock",
            "label": "Buy Stock Equivalent P/L",
            "risk_key": "buy_stock_risk",
            "pl_key": "buy_stock_pl",
            "net_key": "buy_stock_net",
            "charges_key": "buy_stock_charges",
        },
        {
            "key": "short_stock",
            "label": "Short Stock Equivalent P/L",
            "risk_key": "short_stock_risk",
            "pl_key": "short_stock_pl",
            "net_key": "short_stock_net",
            "charges_key": "short_stock_charges",
        },
    ]
    return [
        column
        for column in scenario_columns
        if any(row.get(column["pl_key"]) is not None for row in rows)
    ]


def max_drawdown(values):
    peak = 0
    cumulative = 0
    drawdown = 0
    for value in values:
        cumulative += value or 0
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def volatility(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def scenario_risk_stats(rows, value_key, risk_key):
    chronological = sorted(rows, key=lambda item: (item["date"] or "", item["bet_no"]))
    values = [row[value_key] for row in chronological if row.get(value_key) is not None]
    returns = [
        row[value_key] / row[risk_key] * 100
        for row in chronological
        if row.get(value_key) is not None and row.get(risk_key)
    ]
    return {
        "max_drawdown": max_drawdown(values) if values else None,
        "max_drawdown_pct": max_drawdown(returns) if returns else None,
        "volatility_pct": volatility(returns),
        "win_rate": (len([value for value in values if value > 0]) / len(values) * 100) if values else None,
        "worst": min(values) if values else None,
    }


def after_tax_profit(value, tax_rate):
    if value is None:
        return None
    if value <= 0:
        return value
    return value * (1 - tax_rate / 100)


def whatif_tax_basis(label):
    if "Stock Equivalent" in label:
        return "STCG", STCG_TAX_RATE
    return "Business", BUSINESS_INCOME_TAX_RATE


def option_whatif_report_summary(rows):
    scenarios = [
        ("Actual strategy", "actual_net", "actual_risk"),
        ("Long Call only", "long_call_net", "long_call_risk"),
        ("Long Put only", "long_put_net", "long_put_risk"),
        ("Buy Stock Equivalent", "buy_stock_net", "buy_stock_risk"),
        ("Short Stock Equivalent", "short_stock_net", "short_stock_risk"),
    ]
    summary = {
        "closed_bets": len(rows),
        "best_label": "No data",
        "best_value": None,
        "actual_value": None,
        "gap_vs_actual": None,
        "insight": "No closed option bets are available for what-if analysis yet.",
        "scenario_cards": [],
    }
    if not rows:
        return summary

    scenario_cards = []
    for label, key, risk_key in scenarios:
        values = [row[key] for row in rows if row.get(key) is not None]
        total = sum(values) if values else None
        capital_values = [row[risk_key] for row in rows if row.get(key) is not None and row.get(risk_key)]
        capital_total = sum(capital_values) if capital_values else None
        capital_avg = (capital_total / len(capital_values)) if capital_values else None
        risk_stats = scenario_risk_stats(rows, key, risk_key)
        tax_label, tax_rate = whatif_tax_basis(label)
        scenario_cards.append(
            {
                "label": label,
                "value": total,
                "count": len(values),
                "avg": (total / len(values)) if values else None,
                "capital_total": capital_total,
                "capital_avg": capital_avg,
                "tax_label": tax_label,
                "tax_rate": tax_rate,
                "after_tax": after_tax_profit(total, tax_rate),
                **risk_stats,
            }
        )

    ranked = [card for card in scenario_cards if card["value"] is not None]
    ranked.sort(key=lambda card: card["value"], reverse=True)
    best = ranked[0] if ranked else None
    actual = next((card for card in scenario_cards if card["label"] == "Actual strategy"), None)
    summary["actual_value"] = actual["value"] if actual else None
    if best:
        summary["best_label"] = best["label"]
        summary["best_value"] = best["value"]
        if actual and actual["value"] is not None and best["label"] != actual["label"]:
            gap = best["value"] - actual["value"]
            summary["gap_vs_actual"] = gap
            summary["insight"] = f"{best['label']} is ahead of the actual strategy by {money(gap)} on net P/L."
        elif actual and actual["value"] is not None:
            summary["insight"] = "The actual strategy is currently the strongest net outcome in this comparison."
        else:
            summary["insight"] = f"{best['label']} is the strongest available scenario in this comparison."

    summary["scenario_cards"] = [card for card in scenario_cards if card["count"]]
    return summary


def option_stock_move_distribution(rows):
    """Group completed option bets into signed one-percent stock-move bands."""
    bands = [
        (None, -5, "<-5%"),
        (-5, -4, "-5% to -4%"),
        (-4, -3, "-4% to -3%"),
        (-3, -2, "-3% to -2%"),
        (-2, -1, "-2% to -1%"),
        (-1, 0, "-1% to 0%"),
        (0, 1, "0% to 1%"),
        (1, 2, "1% to 2%"),
        (2, 3, "2% to 3%"),
        (3, 4, "3% to 4%"),
        (4, 5, "4% to 5%"),
        (5, None, "5%+"),
    ]
    result = [{"label": label, "count": 0, "up": 0, "down": 0} for _start, _end, label in bands]
    missing = 0
    for row in rows:
        move = row.get("stock_move_pct")
        if move is None:
            missing += 1
            continue
        value = float(move)
        for index, (start, end, _label) in enumerate(bands):
            if (start is None or value >= start) and (end is None or value < end):
                result[index]["count"] += 1
                if move > 0:
                    result[index]["up"] += 1
                elif move < 0:
                    result[index]["down"] += 1
                break
    measured = sum(item["count"] for item in result)
    max_count = max((item["count"] for item in result), default=0)
    for item in result:
        item["share"] = (item["count"] / measured * 100) if measured else 0
        item["height"] = max(4, item["count"] / max_count * 100) if max_count else 0
    return {
        "bands": result,
        "measured": measured,
        "up": sum(item["up"] for item in result),
        "down": sum(item["down"] for item in result),
        "flat": measured - sum(item["up"] for item in result) - sum(item["down"] for item in result),
        "missing": missing,
    }


def option_growth_chart_data(strategies):
    closed = [
        row for row in strategies
        if (row["status"] or "").lower() == "closed"
        and row["strategy_pl"] is not None
    ]
    closed.sort(key=lambda row: (row["sell_date"] or row["buy_date"] or "", row["id"]))

    total = 0
    points = []
    for row in closed:
        pl_value = row["strategy_pl"] or 0
        total += pl_value
        points.append(
            {
                "date": row["sell_date"] or row["buy_date"],
                "label": f"{row['strategy_type']} #{row['bet_no'] or row['strategy_no']}",
                "underlying": row["underlying"],
                "pl": round(pl_value, 2),
                "value": round(total, 2),
                "outcome": "win" if pl_value > 0 else "loss" if pl_value < 0 else "flat",
            }
        )
    return points


@app.route("/options")
@login_required
def options_dashboard():
    trade_groups = list_trade_groups(active_only=True)
    requested_trade_group_id = request.args.get("trade_group_id", type=int)
    requested_trade_group = get_trade_group(requested_trade_group_id) if requested_trade_group_id else None
    if requested_trade_group and not any(group["id"] == requested_trade_group["id"] for group in trade_groups):
        trade_groups.append(requested_trade_group)
    selected_trade_group = requested_trade_group or (trade_groups[0] if trade_groups else None)
    selected_trade_group_id = selected_trade_group["id"] if selected_trade_group else None
    selected_strategy_type = request.args.get("strategy_type") or None
    backfill_missing_option_stock_moves()
    selected_group_type = (selected_trade_group.get("group_type") if selected_trade_group else "LIVE") or "LIVE"
    selected_group_reportable = selected_group_type.upper() == "LIVE" and bool(selected_trade_group and selected_trade_group["is_active"])
    selected_group_analysable = bool(selected_trade_group and selected_trade_group["is_active"])
    strategies = []
    if selected_group_analysable:
        strategies = list_option_strategies(
            strategy_type=selected_strategy_type,
            trade_group_id=selected_trade_group_id,
            reportable_only=False,
        )
    for row in strategies:
        row["display_pl_pct"] = option_strategy_return_pct(row)
    return render_template(
        "options_dashboard.html",
        strategies=strategies,
        metrics=option_dashboard_metrics(strategies),
        growth_chart=option_growth_chart_data(strategies),
        trade_groups=trade_groups,
        selected_trade_group=selected_trade_group,
        selected_trade_group_id=selected_trade_group_id,
        selected_group_reportable=selected_group_reportable,
        selected_group_analysable=selected_group_analysable,
        strategy_types=OPTION_STRATEGY_TYPES,
        selected_strategy_type=selected_strategy_type,
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title=f"{selected_trade_group['name']} Option Bets" if selected_trade_group else "Option Strategies",
    )


@app.route("/options/strategy/<path:strategy_type>")
@login_required
def option_strategy_performance(strategy_type):
    return redirect(url_for("options_dashboard", strategy_type=strategy_type))


@app.route("/options/what-if")
@login_required
def options_whatif_report():
    trade_groups = list_trade_groups(active_only=True)
    requested_trade_group_id = request.args.get("trade_group_id", type=int)
    requested_trade_group = get_trade_group(requested_trade_group_id) if requested_trade_group_id else None
    if requested_trade_group and not any(group["id"] == requested_trade_group["id"] for group in trade_groups):
        trade_groups.append(requested_trade_group)
    selected_trade_group = requested_trade_group or (trade_groups[0] if trade_groups else None)
    selected_trade_group_id = selected_trade_group["id"] if selected_trade_group else None
    selected_strategy_type = request.args.get("strategy_type") or None

    backfill_missing_option_stock_moves()
    selected_group_type = (selected_trade_group.get("group_type") if selected_trade_group else "LIVE") or "LIVE"
    selected_group_reportable = selected_group_type.upper() == "LIVE" and bool(selected_trade_group and selected_trade_group["is_active"])
    selected_group_analysable = bool(selected_trade_group and selected_trade_group["is_active"])
    strategies = []
    if selected_group_analysable:
        strategies = list_option_strategies(
            strategy_type=selected_strategy_type,
            trade_group_id=selected_trade_group_id,
            reportable_only=False,
        )
    rows = option_whatif_report_rows(strategies)

    return render_template(
        "options_whatif_report.html",
        rows=rows,
        summary=option_whatif_report_summary(rows),
        move_distribution=option_stock_move_distribution(rows),
        visible_columns=option_whatif_visible_columns(rows),
        trade_groups=trade_groups,
        selected_trade_group=selected_trade_group,
        selected_trade_group_id=selected_trade_group_id,
        selected_group_reportable=selected_group_reportable,
        selected_group_analysable=selected_group_analysable,
        strategy_types=OPTION_STRATEGY_TYPES,
        selected_strategy_type=selected_strategy_type,
        money=money,
        number=number,
        pct=pct,
        rate=rate,
        pl_class=pl_class,
        title="Options What-if Report",
    )


def trade_group_form_values(form):
    name = (form.get("name") or "").strip()
    code = re.sub(r"[^A-Z0-9_-]", "", (form.get("code") or "").strip().upper())
    group_type = "PAPER" if form.get("is_paper_trade") == "on" else "LIVE"
    description = (form.get("description") or "").strip() or None
    display_order = parse_int(form.get("display_order")) or 0
    if not name:
        raise ValueError("Group Name is required.")
    if not code:
        raise ValueError("Short Code is required and must contain letters or numbers.")
    if len(code) > 12:
        raise ValueError("Short Code must be 12 characters or fewer.")
    if group_type not in TRADE_GROUP_TYPES:
        raise ValueError("Group Type must be Live or Paper Trade.")
    return name, code, description, display_order, group_type


def stock_trade_group_form_values(form):
    name = (form.get("name") or "").strip()
    code = re.sub(r"[^A-Z0-9_-]", "", (form.get("code") or "").strip().upper())
    description = (form.get("description") or "").strip() or None
    display_order = parse_int(form.get("display_order")) or 0
    group_type = "PAPER" if form.get("is_paper_trade") == "on" else "LIVE"
    if not name:
        raise ValueError("Group Name is required.")
    if not code:
        raise ValueError("Short Code is required and must contain letters or numbers.")
    if len(code) > 12:
        raise ValueError("Short Code must be 12 characters or fewer.")
    return name, code, description, display_order, group_type


@app.route("/stock/groups", methods=["GET", "POST"])
@login_required
def manage_stock_trade_groups():
    if request.method == "POST":
        try:
            name, code, description, display_order, group_type = stock_trade_group_form_values(request.form)
            create_stock_trade_group(name, code, description, display_order, group_type)
            flash(f"Stock Trade Group {name} created.", "success")
            return redirect(url_for("manage_stock_trade_groups"))
        except Exception as exc:
            app.logger.exception("Could not create stock trade group")
            flash(f"Could not create Stock Trade Group: {exc}", "error")
    return render_template(
        "stock_trade_groups.html",
        trade_groups=list_stock_trade_groups(),
        editing_group=None,
        title="Manage Stock Trade Groups",
    )


@app.route("/stock/groups/<int:group_id>/edit", methods=["GET", "POST"])
@login_required
def edit_stock_trade_group(group_id):
    group = get_stock_trade_group(group_id)
    if not group:
        flash("Stock Trade Group not found.", "error")
        return redirect(url_for("manage_stock_trade_groups"))
    if request.method == "POST":
        try:
            name, code, description, display_order, group_type = stock_trade_group_form_values(request.form)
            update_stock_trade_group(group_id, name, code, description, display_order, group_type)
            flash(f"Stock Trade Group {name} updated.", "success")
            return redirect(url_for("manage_stock_trade_groups"))
        except Exception as exc:
            app.logger.exception("Could not update stock trade group: id=%s", group_id)
            flash(f"Could not update Stock Trade Group: {exc}", "error")
    return render_template(
        "stock_trade_groups.html",
        trade_groups=list_stock_trade_groups(),
        editing_group=group,
        title=f"Edit Stock Trade Group {group['name']}",
    )


@app.post("/stock/groups/<int:group_id>/toggle")
@login_required
def toggle_stock_trade_group(group_id):
    group = get_stock_trade_group(group_id)
    if not group:
        flash("Stock Trade Group not found.", "error")
    else:
        set_stock_trade_group_active(group_id, not group["is_active"])
        state = "restored" if not group["is_active"] else "archived"
        flash(f"Stock Trade Group {group['name']} {state}.", "success")
    return redirect(url_for("manage_stock_trade_groups"))


@app.post("/stock/groups/<int:group_id>/delete-paper")
@login_required
def delete_paper_stock_trade_group_route(group_id):
    try:
        deleted = delete_paper_stock_trade_group(group_id)
        flash(
            f"Permanently deleted Paper Trade group {deleted['name']} and {deleted['position_count']} associated bet{'s' if deleted['position_count'] != 1 else ''}.",
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("manage_stock_trade_groups"))


@app.route("/stock/alerts", methods=["GET", "POST"])
@login_required
def manage_stock_alerts():
    form_values = None
    if request.method == "POST":
        try:
            data = stock_alert_from_form(request.form)
            alert_id = create_stock_price_alert(data)
            app.logger.info(
                "Stock alert created: id=%s ticker=%s direction=%s trigger_price=%s channel=%s",
                alert_id,
                data["ticker"],
                data["direction"],
                data["trigger_price"],
                data["channel"],
            )
            flash(f"Stock alert created for {data['ticker']} at {format_indian_inr(data['trigger_price'])}.", "success")
            return redirect(url_for("manage_stock_alerts"))
        except Exception as exc:
            app.logger.exception("Could not create stock alert")
            form_values = stock_alert_form_values(request.form)
            flash(f"Could not create stock alert: {exc}", "error")

    return render_template(
        "stock_alerts.html",
        editing_alert=None,
        form_values=form_values,
        alerts=list_stock_price_alerts(),
        directions=STOCK_ALERT_DIRECTIONS,
        channels=STOCK_ALERT_CHANNELS,
        money=money,
        title="Stock Alerts",
    )


@app.route("/stock/alerts/<int:alert_id>/edit", methods=["GET", "POST"])
@login_required
def edit_stock_alert(alert_id):
    alert = get_stock_price_alert(alert_id)
    if not alert:
        flash("Stock alert not found.", "error")
        return redirect(url_for("manage_stock_alerts"))

    form_values = None
    if request.method == "POST":
        try:
            data = stock_alert_from_form(request.form)
            update_stock_price_alert(alert_id, data)
            app.logger.info(
                "Stock alert updated: id=%s ticker=%s direction=%s trigger_price=%s channel=%s active=%s",
                alert_id,
                data["ticker"],
                data["direction"],
                data["trigger_price"],
                data["channel"],
                data["is_active"],
            )
            flash(f"Stock alert updated for {data['ticker']}.", "success")
            return redirect(url_for("manage_stock_alerts"))
        except Exception as exc:
            app.logger.exception("Could not update stock alert: id=%s", alert_id)
            form_values = stock_alert_form_values(request.form)
            flash(f"Could not update stock alert: {exc}", "error")

    return render_template(
        "stock_alerts.html",
        editing_alert=alert,
        form_values=form_values,
        alerts=list_stock_price_alerts(),
        directions=STOCK_ALERT_DIRECTIONS,
        channels=STOCK_ALERT_CHANNELS,
        money=money,
        title=f"Edit Stock Alert {alert['ticker']}",
    )


@app.post("/stock/alerts/<int:alert_id>/delete")
@login_required
def delete_stock_alert(alert_id):
    alert = get_stock_price_alert(alert_id)
    if not alert:
        flash("Stock alert not found.", "error")
    else:
        delete_stock_price_alert(alert_id)
        app.logger.info("Stock alert deleted: id=%s ticker=%s", alert_id, alert["ticker"])
        flash(f"Deleted stock alert for {alert['ticker']}.", "success")
    return redirect(url_for("manage_stock_alerts"))


@app.route("/options/groups", methods=["GET", "POST"])
@login_required
def manage_trade_groups():
    if request.method == "POST":
        try:
            name, code, description, display_order, group_type = trade_group_form_values(request.form)
            group_id = create_trade_group(name, code, description, display_order, group_type)
            flash(f"Trade Group {name} created.", "success")
            return redirect(url_for("manage_trade_groups"))
        except Exception as exc:
            app.logger.exception("Could not create trade group")
            flash(f"Could not create Trade Group: {exc}", "error")
    return render_template(
        "option_trade_groups.html",
        trade_groups=list_trade_groups(),
        editing_group=None,
        group_types=TRADE_GROUP_TYPES,
        title="Manage Option Trade Groups",
    )


@app.route("/options/groups/<int:group_id>/edit", methods=["GET", "POST"])
@login_required
def edit_trade_group(group_id):
    group = get_trade_group(group_id)
    if not group:
        flash("Trade Group not found.", "error")
        return redirect(url_for("manage_trade_groups"))
    if request.method == "POST":
        try:
            name, code, description, display_order, group_type = trade_group_form_values(request.form)
            update_trade_group(group_id, name, code, description, display_order, group_type)
            flash(f"Trade Group {name} updated.", "success")
            return redirect(url_for("manage_trade_groups"))
        except Exception as exc:
            app.logger.exception("Could not update trade group: id=%s", group_id)
            flash(f"Could not update Trade Group: {exc}", "error")
    return render_template(
        "option_trade_groups.html",
        trade_groups=list_trade_groups(),
        editing_group=group,
        group_types=TRADE_GROUP_TYPES,
        title=f"Edit Trade Group {group['name']}",
    )


@app.post("/options/groups/<int:group_id>/toggle")
@login_required
def toggle_trade_group(group_id):
    group = get_trade_group(group_id)
    if not group:
        flash("Trade Group not found.", "error")
    else:
        set_trade_group_active(group_id, not group["is_active"])
        state = "restored" if not group["is_active"] else "archived"
        flash(f"Trade Group {group['name']} {state}.", "success")
    return redirect(url_for("manage_trade_groups"))


@app.post("/options/groups/<int:group_id>/delete")
@login_required
def delete_trade_group_route(group_id):
    group = get_trade_group(group_id)
    if not group:
        flash("Trade Group not found.", "error")
        return redirect(url_for("manage_trade_groups"))

    try:
        result = delete_trade_group(group_id)
        if result["deleted"]:
            flash(
                f"Trade Group {group['name']} deleted with {result['strategy_count']} option bets.",
                "success",
            )
        else:
            flash("Trade Group could not be deleted.", "error")
    except Exception as exc:
        app.logger.exception("Could not delete trade group: id=%s", group_id)
        flash(f"Could not delete Trade Group: {exc}", "error")
    return redirect(url_for("manage_trade_groups"))


@app.route("/options/new", methods=["GET", "POST"])
@login_required
def new_option_strategy():
    trade_groups = list_trade_groups(active_only=True)
    if not trade_groups:
        flash("Create a Trade Group before adding an option bet.", "error")
        return redirect(url_for("manage_trade_groups"))
    requested_group_id = request.args.get("trade_group_id", type=int)
    default_trade_group_id = requested_group_id if any(
        group["id"] == requested_group_id for group in trade_groups
    ) else trade_groups[0]["id"]
    if request.method == "POST":
        try:
            data, legs = option_strategy_from_form(request.form)
            strategy_id = create_option_strategy(data, legs)
            app.logger.info("Option strategy saved: id=%s type=%s underlying=%s", strategy_id, data["strategy_type"], data["underlying"])
            flash("Option strategy saved.", "success")
            return redirect(url_for("options_dashboard", trade_group_id=data["trade_group_id"]))
        except Exception as exc:
            app.logger.exception("Could not save option strategy")
            flash(f"Could not save option strategy: {exc}", "error")

    return render_template(
        "option_strategy_form.html",
        trade_groups=trade_groups,
        default_trade_group_id=default_trade_group_id,
        strategy_types=OPTION_STRATEGY_TYPES,
        statuses=OPTION_STATUS_OPTIONS,
        next_bet_numbers=next_option_bet_numbers([group["id"] for group in trade_groups]),
        next_strategy_no=next_strategy_no(),
        default_buy_date=datetime.now().date().isoformat(),
        title="Add Option Strategy",
    )


@app.route("/options/<int:strategy_id>/edit", methods=["GET", "POST"])
@login_required
def edit_option_strategy(strategy_id):
    strategy, legs = get_option_strategy(strategy_id)
    if strategy is None:
        flash("Option strategy not found.", "error")
        return redirect(url_for("options_dashboard"))
    trade_groups = list_trade_groups(active_only=True)
    if not any(group["id"] == strategy["trade_group_id"] for group in trade_groups):
        current_group = get_trade_group(strategy["trade_group_id"])
        if current_group:
            trade_groups.append(current_group)

    if request.method == "POST":
        validate_csrf()
        try:
            data, updated_legs = option_strategy_from_form(request.form)
            update_option_strategy(strategy_id, data, updated_legs)
            app.logger.info("Option strategy updated: id=%s type=%s underlying=%s", strategy_id, data["strategy_type"], data["underlying"])
            flash("Option strategy updated.", "success")
            return redirect(url_for("options_dashboard", trade_group_id=data["trade_group_id"]))
        except Exception as exc:
            app.logger.exception("Could not update option strategy")
            flash(f"Could not update option strategy: {exc}", "error")
            strategy = dict(strategy)
            strategy.update(
                {
                    "strategy_no": parse_int(request.form.get("strategy_no")) or strategy.get("strategy_no"),
                    "bet_no": parse_int(request.form.get("bet_no")) or strategy.get("bet_no"),
                    "trade_group_id": parse_int(request.form.get("trade_group_id")) or strategy.get("trade_group_id"),
                    "strategy_type": request.form.get("strategy_type") or strategy.get("strategy_type"),
                    "underlying": request.form.get("underlying") or strategy.get("underlying"),
                    "buy_date": request.form.get("buy_date") or strategy.get("buy_date"),
                    "sell_date": request.form.get("sell_date") or strategy.get("sell_date"),
                    "expiry_date": request.form.get("expiry_date") or strategy.get("expiry_date"),
                    "status": request.form.get("status") or strategy.get("status"),
                    "notes": request.form.get("notes") or "",
                    "is_test_data": False,
                }
            )
            legs = option_legs_from_request(request.form)

    return render_template(
        "option_strategy_form.html",
        strategy=strategy,
        legs=legs,
        trade_groups=trade_groups,
        default_trade_group_id=strategy["trade_group_id"],
        strategy_types=OPTION_STRATEGY_TYPES,
        statuses=OPTION_STATUS_OPTIONS,
        next_bet_numbers=next_option_bet_numbers([group["id"] for group in trade_groups]),
        next_strategy_no=strategy["strategy_no"],
        default_buy_date=strategy["buy_date"],
        title=f"Edit Option Strategy {strategy['strategy_no']}",
    )


@app.route("/options/<int:strategy_id>")
@login_required
def option_strategy_detail(strategy_id):
    strategy, legs = get_option_strategy(strategy_id)
    if strategy is None:
        flash("Option strategy not found.", "error")
        return redirect(url_for("options_dashboard"))
    if (strategy["trade_group_type"] or "LIVE").upper() != "LIVE":
        group_type = TRADE_GROUP_TYPES.get((strategy["trade_group_type"] or "LIVE").upper(), strategy["trade_group_type"])
        flash(f"{strategy['trade_group_name']} is marked as {group_type}; it is excluded from option reports.", "error")
        return redirect(url_for("manage_trade_groups"))
    strategy_context = {**dict(strategy), "legs": [dict(leg) for leg in legs]}
    return render_template(
        "option_strategy_detail.html",
        strategy=strategy,
        legs=legs,
        comparison=option_bet_comparison(strategy, legs),
        display_pl_pct=option_strategy_return_pct(strategy_context),
        display_max_risk=calculated_option_max_risk(strategy_context) or strategy["max_risk"],
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title=f"Option Strategy {strategy['strategy_no']}",
    )


@app.post("/options/<int:strategy_id>/delete")
@login_required
def delete_option_strategy_route(strategy_id):
    delete_option_strategy(strategy_id)
    flash("Option strategy removed.", "success")
    return redirect(url_for("options_dashboard"))


@app.route("/export/excel")
@login_required
def export_excel():
    positions = list_positions()
    export_path = export_positions_to_excel(positions)
    app.logger.info("Excel export generated: path=%s positions=%s", export_path, len(positions))
    return send_file(
        export_path,
        as_attachment=True,
        download_name="Portfolio.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/add-bet", methods=["GET", "POST"])
@app.route("/positions/new", methods=["GET", "POST"])
@login_required
def new_position():
    form_values = None
    stock_trade_groups = list_stock_trade_groups(active_only=True)
    default_stock_group = get_default_stock_trade_group()
    requested_group_id = request.args.get("trade_group_id", type=int)
    default_stock_group_id = requested_group_id if any(
        group["id"] == requested_group_id for group in stock_trade_groups
    ) else (default_stock_group["id"] if default_stock_group else (stock_trade_groups[0]["id"] if stock_trade_groups else None))
    if request.method == "POST":
        try:
            data = position_from_form(request.form)
            position_id = create_position(data)
            app.logger.info("Bet saved: bet_no=%s ticker=%s", data.get("bet_no"), data.get("ticker"))
            if data.get("current_price") is None:
                price, error = refresh_price_after_save(position_id, data["ticker"])
                if error:
                    flash(f"Bet saved, but current price could not be fetched: {error}", "error")
                else:
                    flash(f"Bet saved and current price updated to {format_indian_inr(price)}.", "success")
            else:
                flash("Bet saved.", "success")
            return redirect(url_for("new_position", trade_group_id=data.get("trade_group_id")))
        except Exception as exc:
            app.logger.exception("Could not save bet")
            form_values = form_values_from_request(request.form)
            default_stock_group_id = parse_int(request.form.get("trade_group_id")) or default_stock_group_id
            flash(f"Could not save bet: {exc}", "error")

    positions = sorted(
        list_positions_for_management(trade_group_id=default_stock_group_id),
        key=management_newest_sort_key,
        reverse=True,
    )
    normalized_dates = normalized_buy_date_map(positions)
    next_bet_numbers = next_stock_bet_numbers([group["id"] for group in stock_trade_groups])
    app.logger.debug("Loaded management positions: count=%s", len(positions))
    return render_template(
        "position_form.html",
        position=None,
        positions=positions,
        normalized_dates=normalized_dates,
        form_values=form_values,
        statuses=STATUS_OPTIONS,
        next_bet_no=next_bet_numbers.get(default_stock_group_id, 1),
        next_bet_numbers=next_bet_numbers,
        stock_trade_groups=stock_trade_groups,
        default_stock_group_id=default_stock_group_id,
        selected_stock_group=next((group for group in stock_trade_groups if group["id"] == default_stock_group_id), None),
        default_buy_date=datetime.now().date().isoformat(),
        is_closed_position=is_closed_position,
        management_position_status=management_position_status,
        display_buy_date=display_buy_date,
        money=money,
        number=number,
        title="Add Bet",
    )


@app.route("/positions/<int:position_id>/edit", methods=["GET", "POST"])
@login_required
def edit_position(position_id):
    position = get_position(position_id)
    if position is None:
        flash("Bet not found.")
        return redirect(url_for("index"))

    form_values = None
    stock_trade_groups = list_stock_trade_groups(active_only=True)
    if position["trade_group_id"] and not any(group["id"] == position["trade_group_id"] for group in stock_trade_groups):
        group = get_stock_trade_group(position["trade_group_id"])
        if group:
            stock_trade_groups.append(group)
    if request.method == "POST":
        try:
            data = position_from_form(request.form)
            update_position(position_id, data)
            app.logger.info("Bet updated: id=%s bet_no=%s ticker=%s", position_id, data.get("bet_no"), data.get("ticker"))
            if data.get("current_price") is None:
                price, error = refresh_price_after_save(position_id, data["ticker"])
                if error:
                    flash(f"Bet updated, but current price could not be fetched: {error}", "error")
                else:
                    flash(f"Bet updated and current price refreshed to {format_indian_inr(price)}.", "success")
            else:
                flash("Bet updated.", "success")
            return redirect(url_for("new_position", trade_group_id=data.get("trade_group_id")))
        except Exception as exc:
            app.logger.exception("Could not update bet: id=%s", position_id)
            form_values = form_values_from_request(request.form)
            flash(f"Could not update bet: {exc}", "error")

    selected_group_id = parse_int(getattr(form_values, "trade_group_id", None)) if form_values else position["trade_group_id"]
    positions = sorted(
        list_positions_for_management(trade_group_id=selected_group_id),
        key=management_newest_sort_key,
        reverse=True,
    )
    normalized_dates = normalized_buy_date_map(positions)
    next_bet_numbers = next_stock_bet_numbers([group["id"] for group in stock_trade_groups])
    return render_template(
        "position_form.html",
        position=position,
        positions=positions,
        normalized_dates=normalized_dates,
        form_values=form_values,
        statuses=STATUS_OPTIONS,
        stock_trade_groups=stock_trade_groups,
        default_stock_group_id=position["trade_group_id"],
        next_bet_numbers=next_bet_numbers,
        selected_stock_group=next((group for group in stock_trade_groups if group["id"] == selected_group_id), None),
        default_buy_date="",
        is_closed_position=is_closed_position,
        management_position_status=management_position_status,
        display_buy_date=display_buy_date,
        money=money,
        number=number,
        title="Edit Bet",
    )


@app.post("/positions/<int:position_id>/delete")
@login_required
def delete_position_route(position_id):
    position = get_position(position_id)
    if position is None:
        flash("Bet not found.")
    else:
        delete_position(position_id)
        app.logger.info("Bet removed: id=%s bet_no=%s ticker=%s", position_id, position["bet_no"], position["ticker"])
        flash(f"Removed bet {position['bet_no']} - {position['ticker']}.")

    return redirect("/add-bet")


@app.post("/positions/<int:position_id>/refresh-price")
@login_required
def refresh_price(position_id):
    position = get_position(position_id)
    if position is None:
        flash("Bet not found.")
        return redirect(url_for("index"))

    try:
        price = latest_price(position["ticker"])
        update_current_price(position_id, price)
        app.logger.info("Price updated: id=%s ticker=%s price=%s", position_id, position["ticker"], price)
        flash(f"Updated {position['ticker']} to {format_indian_inr(price)}.")
    except Exception as exc:
        app.logger.exception("Could not update price: id=%s ticker=%s", position_id, position["ticker"])
        flash(f"Could not update price: {exc}")

    return redirect(request.referrer or url_for("new_position"))


register_fund_management(app)


if __name__ == "__main__":
    app.run(host=APP_BIND_HOST, debug=False, port=APP_PORT, use_reloader=False)
