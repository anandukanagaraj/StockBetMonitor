import calendar
import logging
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import create_position, delete_position, get_position, init_db, list_positions, list_positions_for_management, list_test_positions, metrics, next_bet_no, update_current_price, update_position
from options_db import create_option_strategy, delete_option_strategy, get_option_strategy, init_options_db, list_option_strategies, list_option_strategies_missing_stock_move, next_option_bet_numbers, next_strategy_no, update_option_stock_move, update_option_strategy
from services.date_utils import display_date as display_shared_date, normalized_buy_date_map, parse_date as parse_shared_date, position_identity
from services.excel_exporter import export_positions_to_excel
from services.index_service import build_index_membership_report
from services.v1_report_generator import ReportGenerator
from services.yahoo_service import historical_stock_move_pct, latest_price


app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY") or "stock-monitor-v3-dev-change-before-hosting"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
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
LONG_STOCK_NOTIONAL = 1_000_000
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
MAX_PRICE_VALUE = 1_000_000
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
if ADMIN_PASSWORD_HASH:
    ACTIVE_PASSWORD_HASH = ADMIN_PASSWORD_HASH
else:
    ACTIVE_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)
    app.logger.warning("Using development admin password. Set ADMIN_PASSWORD_HASH before hosting.")


def is_authenticated():
    return session.get("admin_authenticated") is True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


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
    }


def money(value):
    if value is None:
        return "-"
    return f"₹{value:,.2f}"


def number(value):
    if value is None:
        return "-"
    return f"{value:,.2f}"


def pct(value):
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def pl_class(value):
    if value is None:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def option_strategy_capital(row):
    capital = row.get("total_trade_capital")
    if not capital:
        capital = sum(
            (leg.get("trade_capital") or ((leg.get("qty") or 0) * (leg.get("buy_price") or 0)))
            for leg in row.get("legs", [])
        )
    return capital or None


def option_strategy_return_pct(row):
    if row.get("strategy_pl") is None:
        return None
    capital = option_strategy_capital(row)
    if not capital:
        return None
    return (row["strategy_pl"] or 0) / capital * 100


def option_leg_type(leg):
    option_type = (leg.get("option_type") or "").strip().upper()
    if option_type in {"CE", "PE"}:
        return option_type
    ticker = (leg.get("ticker") or "").strip().upper()
    if ticker.endswith(" CE"):
        return "CE"
    if ticker.endswith(" PE"):
        return "PE"
    return option_type


def best_scenario_label(scenarios):
    available = [
        (label, value)
        for label, value in scenarios
        if value is not None
    ]
    if not available:
        return "-"
    return max(available, key=lambda item: item[1])[0]


def long_stock_pl_from_move(stock_move_pct):
    if stock_move_pct is None:
        return None
    return LONG_STOCK_NOTIONAL * stock_move_pct / 100


def option_leg_order_count(leg):
    orders = 0
    if leg.get("buy_price") is not None or leg.get("trade_capital") is not None:
        orders += 1
    if leg.get("sell_price") is not None:
        orders += 1
    return orders


def option_brokerage(order_count):
    return order_count * BROKERAGE_PER_ORDER


def rounded_charge(value):
    if value is None:
        return None
    return round(value)


def cost_breakdown(brokerage=0, stt=0, transaction=0, sebi=0, stamp=0, dp=0):
    gst = (brokerage + transaction + sebi) * GST_RATE
    total = brokerage + stt + transaction + sebi + stamp + gst + dp
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "transaction": round(transaction, 2),
        "sebi": round(sebi, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "dp": round(dp, 2),
        "total": round(total, 2),
    }


def empty_costs():
    return cost_breakdown()


def add_costs(left, right):
    left = left or {}
    right = right or {}
    return {
        "brokerage": round((left.get("brokerage") or 0) + (right.get("brokerage") or 0), 2),
        "stt": round((left.get("stt") or 0) + (right.get("stt") or 0), 2),
        "transaction": round((left.get("transaction") or 0) + (right.get("transaction") or 0), 2),
        "sebi": round((left.get("sebi") or 0) + (right.get("sebi") or 0), 2),
        "stamp": round((left.get("stamp") or 0) + (right.get("stamp") or 0), 2),
        "gst": round((left.get("gst") or 0) + (right.get("gst") or 0), 2),
        "dp": round((left.get("dp") or 0) + (right.get("dp") or 0), 2),
        "total": round((left.get("total") or 0) + (right.get("total") or 0), 2),
    }


def option_leg_turnover(leg):
    qty = leg.get("qty") or 0
    entry_value = (leg.get("buy_price") or 0) * qty
    exit_value = (leg.get("sell_price") or 0) * qty if leg.get("sell_price") is not None else 0
    return entry_value + exit_value


def option_leg_buy_turnover(leg):
    qty = leg.get("qty") or 0
    action = (leg.get("action") or "BUY").upper()
    if action == "BUY":
        return (leg.get("buy_price") or 0) * qty
    return (leg.get("sell_price") or 0) * qty if leg.get("sell_price") is not None else 0


def option_leg_stt(leg):
    qty = leg.get("qty") or 0
    action = (leg.get("action") or "BUY").upper()
    if action == "SELL":
        return rounded_charge((leg.get("buy_price") or 0) * qty * OPTION_STT_RATE)
    if leg.get("sell_price") is not None:
        return rounded_charge((leg.get("sell_price") or 0) * qty * OPTION_STT_RATE)
    return 0


def option_strategy_stt(legs):
    return sum(option_leg_stt(leg) or 0 for leg in legs)


def option_strategy_brokerage(legs):
    return option_brokerage(sum(option_leg_order_count(leg) for leg in legs))


def option_strategy_costs(legs):
    brokerage = option_strategy_brokerage(legs)
    stt = option_strategy_stt(legs)
    turnover = sum(option_leg_turnover(leg) for leg in legs)
    buy_turnover = sum(option_leg_buy_turnover(leg) for leg in legs)
    return cost_breakdown(
        brokerage=brokerage,
        stt=stt,
        transaction=turnover * OPTION_TRANSACTION_RATE,
        sebi=turnover * SEBI_RATE,
        stamp=buy_turnover * OPTION_STAMP_RATE,
    )


def single_leg_simulation_brokerage(legs, option_type):
    eligible_legs = [
        leg for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and option_leg_type(leg) == option_type
        and leg.get("achieved_pl") is not None
    ]
    return option_brokerage(len(eligible_legs) * 2) if eligible_legs else None


def single_leg_simulation_stt(legs, option_type):
    eligible_legs = [
        leg for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and option_leg_type(leg) == option_type
        and leg.get("achieved_pl") is not None
        and leg.get("sell_price") is not None
    ]
    if not eligible_legs:
        return None
    return sum(rounded_charge((leg.get("sell_price") or 0) * (leg.get("qty") or 0) * OPTION_STT_RATE) for leg in eligible_legs)


def single_leg_simulation_costs(legs, option_type):
    eligible_legs = [
        leg for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and option_leg_type(leg) == option_type
        and leg.get("achieved_pl") is not None
    ]
    if not eligible_legs:
        return None
    brokerage = option_brokerage(len(eligible_legs) * 2)
    stt = single_leg_simulation_stt(eligible_legs, option_type) or 0
    turnover = sum(option_leg_turnover(leg) for leg in eligible_legs)
    buy_turnover = sum(option_leg_buy_turnover(leg) for leg in eligible_legs)
    return cost_breakdown(
        brokerage=brokerage,
        stt=stt,
        transaction=turnover * OPTION_TRANSACTION_RATE,
        sebi=turnover * SEBI_RATE,
        stamp=buy_turnover * OPTION_STAMP_RATE,
    )


def long_stock_simulation_brokerage(stock_move_pct):
    return 0 if stock_move_pct is not None else None


def long_stock_simulation_stt(stock_move_pct):
    if stock_move_pct is None:
        return None
    buy_stt = LONG_STOCK_NOTIONAL * EQUITY_DELIVERY_STT_RATE
    sell_value = LONG_STOCK_NOTIONAL * (1 + stock_move_pct / 100)
    sell_stt = max(0, sell_value) * EQUITY_DELIVERY_STT_RATE
    return rounded_charge(buy_stt + sell_stt)


def long_stock_simulation_costs(stock_move_pct):
    if stock_move_pct is None:
        return None
    buy_value = LONG_STOCK_NOTIONAL
    sell_value = max(0, LONG_STOCK_NOTIONAL * (1 + stock_move_pct / 100))
    turnover = buy_value + sell_value
    return cost_breakdown(
        brokerage=0,
        stt=long_stock_simulation_stt(stock_move_pct) or 0,
        transaction=turnover * EQUITY_DELIVERY_TRANSACTION_RATE,
        sebi=turnover * SEBI_RATE,
        stamp=buy_value * EQUITY_DELIVERY_STAMP_RATE,
        dp=EQUITY_DP_CHARGE,
    )


def net_after_costs(pl_value, costs):
    if pl_value is None:
        return None
    if isinstance(costs, dict):
        return pl_value - (costs.get("total") or 0)
    return pl_value - (costs or 0)


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
    long_stock = long_stock_pl_from_move(strategy["stock_move_pct"])
    actual_costs = option_strategy_costs(legs)
    long_call_costs = single_leg_simulation_costs(legs, "CE")
    long_put_costs = single_leg_simulation_costs(legs, "PE")
    long_stock_costs = long_stock_simulation_costs(strategy["stock_move_pct"])
    actual_net = net_after_costs(actual, actual_costs)
    scenarios = [
        ("Actual Strategy", actual, actual_costs),
        ("Long Call Only", long_call, long_call_costs),
        ("Long Put Only", long_put, long_put_costs),
        ("Long Stock 10L", long_stock, long_stock_costs),
    ]
    return {
        "scenarios": [
            {
                "label": label,
                "pl": value,
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
            for label, value, costs in scenarios
        ],
        "best": best_scenario_label(
            [(label, net_after_costs(value, costs)) for label, value, costs in scenarios]
        ),
    }


def backfill_missing_option_stock_moves():
    rows = list_option_strategies_missing_stock_move()
    if not rows:
        return 0

    updated = 0
    for row in rows:
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
        except Exception:
            app.logger.exception(
                "Could not backfill option stock move: id=%s underlying=%s buy_date=%s sell_date=%s",
                row["id"],
                row["underlying"],
                row["buy_date"],
                row["sell_date"],
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
        if remaining >= 0:
            near_targets.append(
                {
                    "ticker": row["ticker"],
                    "current": row["current_price"],
                    "target": row["target_price"],
                    "remaining": remaining,
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


def recent_stock_activity(positions, limit=5):
    rows = sorted(
        positions,
        key=lambda row: row["updated_at"] or row["created_at"] or row["buy_date"] or "",
        reverse=True,
    )
    activity = []
    for row in rows[:limit]:
        closed = is_closed_position(row)
        pl_value = realized_pl(row) if closed else None
        activity.append(
            {
                "label": f"Bet {row['bet_no'] or '-'}",
                "title": row["ticker"],
                "meta": "Closed" if closed else "Open",
                "date": row["sell_date"] if closed and row["sell_date"] else row["buy_date"],
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
    for item in transactions:
        if item["date"].year == selected_year and item["date"].month == selected_month:
            by_day[item["date"].day].append(item)

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
        review_report = build_index_membership_report(open_bets, positions)
        review_summary = review_report["summary_cards"]
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


def render_v1_report_from_db(show_target_price=False):
    rows = list_positions()
    log_ticker_snapshot(rows, show_target_price)
    app.logger.debug(
        "Rendering report from DB: rows=%s show_target_price=%s",
        len(rows),
        show_target_price,
    )
    positions = [db_row_to_v1_position(row) for row in rows]
    report_path = ReportGenerator().generate_html(positions, show_target_price=show_target_price)
    app.logger.info("Report generated: path=%s show_target_price=%s", report_path, show_target_price)
    html = Path(report_path).read_text(encoding="utf-8")
    app.logger.debug("Report HTML loaded: bytes=%s", len(html.encode("utf-8")))
    return html


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


def position_from_form(form):
    sell_price = parse_float(form.get("sell_price"), field_name="Sell Price")
    current_price = parse_float(form.get("current_price"), field_name="Current Price")
    qty = parse_float(form.get("qty"), required=True, field_name="Quantity")
    buy_price = parse_float(form.get("buy_price"), required=True, field_name="Buy Price")
    target_price = parse_float(form.get("target_price"), required=True, field_name="Target Price")
    stop_loss = parse_float(form.get("stop_loss"), field_name="Stop Loss")
    sell_date = form.get("sell_date") or None
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

    data = {
        "bet_no": parse_int(form.get("bet_no")),
        "ticker": form["ticker"],
        "buy_date": form["buy_date"],
        "qty": qty,
        "buy_price": buy_price,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "current_price": current_price,
        "sell_date": sell_date,
        "sell_price": sell_price,
        "status": status,
        "notes": None,
        "is_test_data": form.get("is_test_data") == "on",
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
        "stock_move_pct": None,
        "long_only_pl": None,
        "notes": form.get("notes") or None,
        "is_test_data": form.get("is_test_data") == "on",
    }

    if not data["underlying"]:
        raise ValueError("Underlying is required.")
    if not data["buy_date"]:
        raise ValueError("Buy Date is required.")

    if sell_date:
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
        "bet_no", "ticker", "buy_date", "qty", "buy_price", "current_price",
        "target_price", "stop_loss", "sell_date", "sell_price", "status", "notes", "is_test_data",
    ]
    return SimpleNamespace(**{field: form.get(field, "") for field in fields})


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


@app.route("/")
@login_required
def index():
    dashboard = build_home_dashboard()
    return render_template(
        "index.html",
        dashboard=dashboard,
        money=money,
        pct=pct,
        pl_class=pl_class,
        title="Dashboard",
    )


@app.route("/report")
def report_masked():
    return Response(render_v1_report_from_db(), mimetype="text/html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == ADMIN_USERNAME and check_password_hash(ACTIVE_PASSWORD_HASH, password):
            session.clear()
            session["admin_authenticated"] = True
            session["admin_username"] = username
            csrf_token()
            app.logger.info("Admin login succeeded: username=%s", username)
            return redirect(request.args.get("next") or url_for("report_targets"))
        app.logger.warning("Admin login failed: username=%s", username)
        flash("Invalid username or password.")

    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


@app.route("/report-targets")
@login_required
def report_targets():
    return Response(render_v1_report_from_db(show_target_price=True), mimetype="text/html")


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


@app.route("/index-membership")
@login_required
def index_membership():
    open_bets = list_positions_for_management()
    report = build_index_membership_report(open_bets, list_positions())
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


def option_dashboard_metrics(strategies):
    closed = [row for row in strategies if (row["status"] or "").lower() == "closed"]
    open_rows = [row for row in strategies if (row["status"] or "").lower() != "closed"]
    wins = [row for row in closed if (row["strategy_pl"] or 0) > 0]
    losses = [row for row in closed if (row["strategy_pl"] or 0) < 0]
    realized_pl = sum(row["strategy_pl"] or 0 for row in closed)
    return_capital = sum(option_strategy_capital(row) or 0 for row in closed if row["strategy_pl"] is not None)
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
                "long_put_pl": 0,
                "long_put_costs": empty_costs(),
                "long_put_count": 0,
                "long_stock_pl": 0,
                "long_stock_costs": empty_costs(),
                "long_stock_count": 0,
                "long_call_trades": [],
                "long_stock_trades": [],
            },
        )
        summary["total"] += 1
        if row["long_call_pl"] is not None:
            costs = single_leg_simulation_costs(row.get("legs") or [], "CE")
            summary["long_call_pl"] += row["long_call_pl"]
            summary["long_call_costs"] = add_costs(summary["long_call_costs"], costs)
            summary["long_call_count"] += 1
            summary["long_call_trades"].append(
                {
                    "bet_no": row["bet_no"] or row["strategy_no"],
                    "underlying": row["underlying"],
                    "date": row["sell_date"] or row["buy_date"],
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
        if row["long_stock_pl"] is not None:
            costs = long_stock_simulation_costs(row["stock_move_pct"])
            summary["long_stock_pl"] += row["long_stock_pl"]
            summary["long_stock_costs"] = add_costs(summary["long_stock_costs"], costs)
            summary["long_stock_count"] += 1
            summary["long_stock_trades"].append(
                {
                    "bet_no": row["bet_no"] or row["strategy_no"],
                    "underlying": row["underlying"],
                    "date": row["sell_date"] or row["buy_date"],
                    "stock_move_pct": row["stock_move_pct"],
                    "pl": row["long_stock_pl"],
                    "brokerage": costs.get("brokerage") if costs else None,
                    "charges": costs.get("total") if costs else None,
                    "net": net_after_costs(row["long_stock_pl"], costs),
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
        summary["best"] = best_scenario_label(
            [
                ("Actual", summary["actual_net"]),
                ("Long Call", summary["long_call_net"]),
                ("Long Put", summary["long_put_net"]),
                ("Long Stock 10L", summary["long_stock_net"]),
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
        "by_strategy": sorted(by_strategy.values(), key=lambda item: item["total"], reverse=True),
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
    selected_strategy_type = request.args.get("strategy_type") or None
    backfill_missing_option_stock_moves()
    strategies = list_option_strategies(strategy_type=selected_strategy_type)
    for row in strategies:
        row["display_pl_pct"] = option_strategy_return_pct(row)
    return render_template(
        "options_dashboard.html",
        strategies=strategies,
        metrics=option_dashboard_metrics(strategies),
        growth_chart=option_growth_chart_data(strategies),
        strategy_types=OPTION_STRATEGY_TYPES,
        selected_strategy_type=selected_strategy_type,
        money=money,
        number=number,
        pct=pct,
        pl_class=pl_class,
        title=f"{selected_strategy_type} Performance" if selected_strategy_type else "Option Strategies",
    )


@app.route("/options/strategy/<path:strategy_type>")
@login_required
def option_strategy_performance(strategy_type):
    return redirect(url_for("options_dashboard", strategy_type=strategy_type))


@app.route("/options/new", methods=["GET", "POST"])
@login_required
def new_option_strategy():
    if request.method == "POST":
        try:
            data, legs = option_strategy_from_form(request.form)
            strategy_id = create_option_strategy(data, legs)
            app.logger.info("Option strategy saved: id=%s type=%s underlying=%s", strategy_id, data["strategy_type"], data["underlying"])
            flash("Option strategy saved.", "success")
            return redirect(url_for("options_dashboard"))
        except Exception as exc:
            app.logger.exception("Could not save option strategy")
            flash(f"Could not save option strategy: {exc}", "error")

    return render_template(
        "option_strategy_form.html",
        strategy_types=OPTION_STRATEGY_TYPES,
        statuses=OPTION_STATUS_OPTIONS,
        next_bet_numbers=next_option_bet_numbers(OPTION_STRATEGY_TYPES),
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

    if request.method == "POST":
        validate_csrf()
        try:
            data, updated_legs = option_strategy_from_form(request.form)
            update_option_strategy(strategy_id, data, updated_legs)
            app.logger.info("Option strategy updated: id=%s type=%s underlying=%s", strategy_id, data["strategy_type"], data["underlying"])
            flash("Option strategy updated.", "success")
            return redirect(url_for("options_dashboard"))
        except Exception as exc:
            app.logger.exception("Could not update option strategy")
            flash(f"Could not update option strategy: {exc}", "error")
            strategy = dict(strategy)
            strategy.update(
                {
                    "strategy_no": parse_int(request.form.get("strategy_no")) or strategy.get("strategy_no"),
                    "bet_no": parse_int(request.form.get("bet_no")) or strategy.get("bet_no"),
                    "strategy_type": request.form.get("strategy_type") or strategy.get("strategy_type"),
                    "underlying": request.form.get("underlying") or strategy.get("underlying"),
                    "buy_date": request.form.get("buy_date") or strategy.get("buy_date"),
                    "sell_date": request.form.get("sell_date") or strategy.get("sell_date"),
                    "expiry_date": request.form.get("expiry_date") or strategy.get("expiry_date"),
                    "status": request.form.get("status") or strategy.get("status"),
                    "notes": request.form.get("notes") or "",
                    "is_test_data": request.form.get("is_test_data") == "on",
                }
            )
            legs = option_legs_from_request(request.form)

    return render_template(
        "option_strategy_form.html",
        strategy=strategy,
        legs=legs,
        strategy_types=OPTION_STRATEGY_TYPES,
        statuses=OPTION_STATUS_OPTIONS,
        next_bet_numbers=next_option_bet_numbers(OPTION_STRATEGY_TYPES),
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
    return render_template(
        "option_strategy_detail.html",
        strategy=strategy,
        legs=legs,
        comparison=option_bet_comparison(strategy, legs),
        long_stock_notional=LONG_STOCK_NOTIONAL,
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
                    flash(f"Bet saved and current price updated to INR {price:,.2f}.", "success")
            else:
                flash("Bet saved.", "success")
            return redirect("/add-bet")
        except Exception as exc:
            app.logger.exception("Could not save bet")
            form_values = form_values_from_request(request.form)
            flash(f"Could not save bet: {exc}", "error")

    positions = list_positions_for_management()
    test_positions = list_test_positions()
    normalized_dates = normalized_buy_date_map(list(positions) + list(test_positions))
    app.logger.debug("Loaded management positions: count=%s", len(positions))
    return render_template(
        "position_form.html",
        position=None,
        positions=positions,
        test_positions=test_positions,
        normalized_dates=normalized_dates,
        form_values=form_values,
        statuses=STATUS_OPTIONS,
        next_bet_no=next_bet_no(),
        default_buy_date=datetime.now().date().isoformat(),
        is_closed_position=is_closed_position,
        display_buy_date=display_buy_date,
        money=money,
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
    if request.method == "POST":
        try:
            data = position_from_form(request.form)
            update_position(position_id, data)
            app.logger.info("Bet updated: id=%s bet_no=%s ticker=%s", position_id, data.get("bet_no"), data.get("ticker"))
            flash("Bet updated.", "success")
            return redirect("/add-bet")
        except Exception as exc:
            app.logger.exception("Could not update bet: id=%s", position_id)
            form_values = form_values_from_request(request.form)
            flash(f"Could not update bet: {exc}", "error")

    positions = list_positions_for_management()
    test_positions = list_test_positions()
    normalized_dates = normalized_buy_date_map(list(positions) + list(test_positions))
    return render_template(
        "position_form.html",
        position=position,
        positions=positions,
        test_positions=test_positions,
        normalized_dates=normalized_dates,
        form_values=form_values,
        statuses=STATUS_OPTIONS,
        default_buy_date="",
        is_closed_position=is_closed_position,
        display_buy_date=display_buy_date,
        money=money,
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
        flash(f"Updated {position['ticker']} to INR {price:,.2f}.")
    except Exception as exc:
        app.logger.exception("Could not update price: id=%s ticker=%s", position_id, position["ticker"])
        flash(f"Could not update price: {exc}")

    return redirect(request.referrer or url_for("new_position"))


if __name__ == "__main__":
    app.run(debug=True, port=5002, use_reloader=False)
