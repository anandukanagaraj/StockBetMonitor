import csv
import io
import json
import logging
import re
import time
from datetime import date
from pathlib import Path

import requests

from services.date_utils import normalized_buy_date_map, parse_date, position_identity
from services.yahoo_service import resolve_symbol


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = BASE_DIR / "data" / "index_membership_snapshot.json"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.niftyindices.com/indices/equity/broad-based-indices",
}

INDEX_DEFINITIONS = [
    ("NIFTY 50", "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"),
    ("NIFTY NEXT 50", "https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv"),
    ("NIFTY 100", "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"),
    ("NIFTY 200", "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"),
    ("NIFTY 500", "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"),
    ("NIFTY MIDCAP 50", "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap50list.csv"),
    ("NIFTY MIDCAP 100", "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap100list.csv"),
    ("NIFTY SMALLCAP 100", "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap100list.csv"),
]

INDEX_PRIORITY = {
    "NIFTY 50": 1,
    "NIFTY NEXT 50": 2,
    "NIFTY 100": 3,
    "NIFTY 200": 4,
    "NIFTY 500": 5,
    "NIFTY MIDCAP 50": 6,
    "NIFTY MIDCAP 100": 7,
    "NIFTY SMALLCAP 100": 8,
}

BROAD_INDEXES = {"NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"}

STATUS_SIGNALS = {
    "stable": {
        "label": "Stable index membership",
        "class": "stable",
        "rank": 1,
    },
    "promoted": {
        "label": "Promoted to a higher index",
        "class": "promoted",
        "rank": 2,
    },
    "downgraded": {
        "label": "Removed from higher index, still in Nifty 500",
        "class": "downgraded",
        "rank": 3,
    },
    "removed": {
        "label": "Removed from Nifty 500",
        "class": "removed",
        "rank": 4,
    },
}

_membership_cache = {"expires_at": 0, "data": {}, "errors": [], "fetched_at": None}
_status_cache = {"expires_at": 0, "data": {}, "errors": []}


def normalize_symbol(value):
    text = (value or "").upper().replace(".NS", "").strip()
    return re.sub(r"[^A-Z0-9]", "", text)


def symbol_for_ticker(ticker):
    try:
        return normalize_symbol(resolve_symbol(ticker))
    except Exception:
        logger.exception("Could not resolve ticker symbol: %s", ticker)
        return normalize_symbol(ticker)


def display_index_name(index_name):
    if not index_name:
        return "None"
    return index_name.replace("NIFTY", "Nifty")


def sort_memberships(memberships):
    return sorted(memberships, key=lambda name: (INDEX_PRIORITY.get(name, 999), name))


def load_previous_snapshot():
    if not SNAPSHOT_PATH.exists():
        return {"as_of": None, "memberships": {}}
    try:
        raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"as_of": None, "memberships": {}}
        raw_memberships = raw.get("memberships") or {}
        memberships = {
            normalize_symbol(symbol): sort_memberships(indexes if isinstance(indexes, list) else [])
            for symbol, indexes in raw_memberships.items()
        }
        return {"as_of": raw.get("as_of"), "memberships": memberships}
    except Exception:
        logger.exception("Could not load index membership snapshot: %s", SNAPSHOT_PATH)
        return {"as_of": None, "memberships": {}}


def save_snapshot(memberships_by_symbol, as_of):
    SNAPSHOT_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "as_of": as_of,
        "source": "NSE official index constituent CSV files",
        "memberships": memberships_by_symbol,
    }
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def broad_memberships(memberships):
    return [name for name in memberships if name in BROAD_INDEXES]


def highest_broad_index(memberships):
    broad = broad_memberships(memberships)
    if not broad:
        return None
    return sort_memberships(broad)[0]


def status_for_symbol(symbol, memberships, previous_memberships, as_of):
    previous = sort_memberships(previous_memberships.get(symbol, []))
    current_highest = highest_broad_index(memberships)
    previous_highest = highest_broad_index(previous)
    current_rank = INDEX_PRIORITY.get(current_highest, 999)
    previous_rank = INDEX_PRIORITY.get(previous_highest, 999)

    if previous and "NIFTY 500" in previous and "NIFTY 500" not in memberships:
        signal = dict(STATUS_SIGNALS["removed"])
        signal["effective_date"] = as_of
        return signal

    if previous_highest and current_highest and current_rank < previous_rank:
        signal = dict(STATUS_SIGNALS["promoted"])
        signal["effective_date"] = as_of
        return signal

    if previous_highest and current_highest and current_rank > previous_rank and "NIFTY 500" in memberships:
        signal = dict(STATUS_SIGNALS["downgraded"])
        signal["effective_date"] = as_of
        return signal

    if memberships:
        signal = dict(STATUS_SIGNALS["stable"])
        signal["effective_date"] = as_of
        return signal

    signal = {
        "label": "Not in tracked indices",
        "class": "none",
        "rank": 5,
        "effective_date": as_of,
    }
    return signal


def is_closed_position(row):
    status = (row["status"] or "").strip().lower()
    return row["sell_price"] is not None or row["sell_date"] is not None or status in {"closed", "target hit", "stop loss hit", "sold"}


def realized_profit(row):
    if row["sell_price"] is None:
        return None
    return (row["sell_price"] - row["buy_price"]) * row["qty"]


def avg_winning_hold_days(rows, normalized_dates):
    winning_hold_days = []
    for row in rows:
        if not is_closed_position(row) or (realized_profit(row) or 0) <= 0:
            continue

        buy_date = normalized_dates.get(position_identity(row)) or parse_date(row["buy_date"])
        sell_date = parse_date(row["sell_date"])
        if buy_date is None or sell_date is None or sell_date < buy_date:
            continue

        winning_hold_days.append((sell_date - buy_date).days)

    if not winning_hold_days:
        return None
    return sum(winning_hold_days) / len(winning_hold_days)


def hold_status_for_row(row, normalized_dates, average_days, today):
    buy_date = normalized_dates.get(position_identity(row)) or parse_date(row["buy_date"])
    if buy_date is None:
        return {
            "label": "Date missing",
            "class": "none",
            "open_days": None,
            "sort_value": 999999,
            "detail": "-",
        }

    open_days = max((today - buy_date).days, 0)
    if average_days is None:
        return {
            "label": "No avg yet",
            "class": "none",
            "open_days": open_days,
            "sort_value": open_days,
            "detail": f"{open_days} days",
        }

    average_label = round(average_days)
    is_within_average = open_days <= average_days
    return {
        "label": "Green" if is_within_average else "Red",
        "class": "green" if is_within_average else "red",
        "open_days": open_days,
        "sort_value": open_days,
        "detail": f"{open_days} / {average_label} days",
    }


def price_drawdown_status(row):
    buy_price = row["buy_price"]
    current_price = row["current_price"]
    if buy_price in (None, 0) or current_price is None:
        return {
            "label": "Price missing",
            "class": "none",
            "drawdown_pct": None,
            "sort_value": 0,
            "detail": "-",
        }

    drawdown_pct = (current_price - buy_price) / buy_price * 100
    is_deep_loss = drawdown_pct <= -75
    return {
        "label": "Down 75%+" if is_deep_loss else "OK",
        "class": "red" if is_deep_loss else "green",
        "drawdown_pct": drawdown_pct,
        "sort_value": abs(drawdown_pct) if is_deep_loss else 0,
        "detail": f"{drawdown_pct:+.2f}%",
    }


def sell_review_status(row, membership_signal, hold_status, price_status):
    reasons = []
    open_days = hold_status.get("open_days")

    if price_status.get("class") == "red":
        reasons.append("Price down 75%+")
    if hold_status.get("class") == "red":
        reasons.append("Avg hold crossed")
    if open_days is not None and open_days >= 365 * 5:
        reasons.append("5 years crossed")
    if membership_signal.get("class") == "removed":
        reasons.append("Removed from index")

    if reasons:
        return {
            "label": "Mandatory review",
            "class": "mandatory",
            "reasons": reasons,
            "detail": " | ".join(reasons),
            "sort_value": len(reasons),
        }

    return {
        "label": "No review",
        "class": "clear",
        "reasons": [],
        "detail": "No mandatory sell-review trigger",
        "sort_value": 0,
    }


def five_year_hold_status(hold_status):
    open_days = hold_status.get("open_days")
    if open_days is None:
        return {
            "label": "Date missing",
            "class": "none",
            "sort_value": 0,
            "detail": "-",
        }

    years = open_days / 365
    is_over_five = open_days >= 365 * 5
    return {
        "label": "Review" if is_over_five else "OK",
        "class": "red" if is_over_five else "green",
        "sort_value": open_days,
        "detail": f"{years:.1f} years",
    }


def fetch_index_memberships(ttl_seconds=21600):
    now = time.time()
    if _membership_cache["data"] and _membership_cache["expires_at"] > now:
        return _membership_cache["data"], _membership_cache["errors"]

    memberships = {}
    errors = []

    for index_name, url in INDEX_DEFINITIONS:
        try:
            response = requests.get(url, headers=NSE_HEADERS, timeout=12)
            response.raise_for_status()
            reader = csv.DictReader(io.StringIO(response.text))
            for row in reader:
                symbol = normalize_symbol(row.get("Symbol") or row.get("SYMBOL"))
                if symbol:
                    memberships.setdefault(symbol, []).append(index_name)
        except Exception as exc:
            logger.exception("Could not fetch index constituents: index=%s url=%s", index_name, url)
            errors.append(f"{index_name}: {exc}")

    _membership_cache.update({
        "expires_at": now + ttl_seconds,
        "data": memberships,
        "errors": errors,
        "fetched_at": time.strftime("%d %b %Y %I:%M %p"),
    })
    return memberships, errors


def fetch_index_statuses(ttl_seconds=300):
    now = time.time()
    if _status_cache["data"] and _status_cache["expires_at"] > now:
        return _status_cache["data"], _status_cache["errors"]

    statuses = {}
    errors = []
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/market-data/live-market-indices",
        })
        session.get("https://www.nseindia.com", timeout=10)
        response = session.get("https://www.nseindia.com/api/allIndices", timeout=12)
        response.raise_for_status()
        for item in response.json().get("data", []):
            name = (item.get("index") or "").upper().strip()
            if not name:
                continue
            last = item.get("last")
            change = item.get("variation")
            pct = item.get("percentChange")
            statuses[name] = {
                "name": name,
                "last": float(str(last).replace(",", "")) if last not in (None, "") else None,
                "change": float(str(change).replace(",", "")) if change not in (None, "") else None,
                "pct": float(str(pct).replace(",", "")) if pct not in (None, "") else None,
                "time": item.get("lastTradeTime") or item.get("timestamp") or "-",
            }
    except Exception as exc:
        logger.exception("Could not fetch index status data")
        errors.append(str(exc))

    _status_cache.update({
        "expires_at": now + ttl_seconds,
        "data": statuses,
        "errors": errors,
    })
    return statuses, errors


def build_dashboard_review_summary(open_rows, all_rows=None):
    if _membership_cache["data"] and _membership_cache["expires_at"] > time.time():
        memberships_by_symbol = _membership_cache["data"]
    else:
        memberships_by_symbol = load_previous_snapshot().get("memberships", {})
        if not memberships_by_symbol:
            memberships_by_symbol, _ = fetch_index_memberships()

    previous_snapshot = load_previous_snapshot()
    previous_memberships = previous_snapshot.get("memberships", {})
    as_of = time.strftime("%d %b %Y")
    all_rows = list(all_rows or open_rows)
    normalized_dates = normalized_buy_date_map(all_rows)
    avg_hold_days = avg_winning_hold_days(all_rows, normalized_dates)
    today = date.today()

    rows = []
    for row in open_rows:
        symbol = symbol_for_ticker(row["ticker"])
        memberships = sort_memberships(memberships_by_symbol.get(symbol, []))
        membership_signal = (
            status_for_symbol(symbol, memberships, previous_memberships, as_of)
            if memberships_by_symbol
            else {
                "label": "Cached membership unavailable",
                "class": "none",
                "rank": 6,
                "effective_date": "-",
            }
        )
        hold_status = hold_status_for_row(row, normalized_dates, avg_hold_days, today)
        price_status = price_drawdown_status(row)
        sell_review = sell_review_status(row, membership_signal, hold_status, price_status)
        rows.append(
            {
                "hold_status": hold_status,
                "membership_signal": membership_signal,
                "price_status": price_status,
                "sell_review": sell_review,
            }
        )

    return {
        "mandatory_sell_review_count": len([row for row in rows if row["sell_review"]["class"] == "mandatory"]),
        "deep_drawdown_count": len([row for row in rows if row["price_status"]["class"] == "red"]),
        "above_avg_hold_count": len([row for row in rows if row["hold_status"]["class"] == "red"]),
        "five_year_hold_count": len([row for row in rows if (row["hold_status"].get("open_days") or 0) >= 365 * 5]),
        "degraded_membership_count": len(
            [row for row in rows if row["membership_signal"]["class"] in {"downgraded", "removed"}]
        ),
        "promoted_membership_count": len([row for row in rows if row["membership_signal"]["class"] == "promoted"]),
    }


def build_index_membership_report(open_rows, all_rows=None):
    memberships_by_symbol, membership_errors = fetch_index_memberships()
    statuses_by_index, status_errors = fetch_index_statuses()
    previous_snapshot = load_previous_snapshot()
    previous_memberships = previous_snapshot.get("memberships", {})
    as_of = time.strftime("%d %b %Y")
    snapshot_as_of = time.strftime("%Y-%m-%d")
    tracked_indices = [name for name, _url in INDEX_DEFINITIONS]
    source_available = bool(memberships_by_symbol)
    all_rows = list(all_rows or open_rows)
    normalized_dates = normalized_buy_date_map(all_rows)
    avg_hold_days = avg_winning_hold_days(all_rows, normalized_dates)
    today = date.today()

    rows = []
    touched_indices = set()
    for row in open_rows:
        symbol = symbol_for_ticker(row["ticker"])
        memberships = sort_memberships(memberships_by_symbol.get(symbol, []))
        for index_name in memberships:
            touched_indices.add(index_name)
        highest_index = highest_broad_index(memberships)
        other_memberships = [name for name in memberships if name != highest_index]
        if source_available:
            membership_signal = status_for_symbol(symbol, memberships, previous_memberships, as_of)
        else:
            membership_signal = {
                "label": "NSE source unavailable",
                "class": "none",
                "rank": 6,
                "effective_date": "-",
            }
        hold_status = hold_status_for_row(row, normalized_dates, avg_hold_days, today)
        five_year_status = five_year_hold_status(hold_status)
        price_status = price_drawdown_status(row)
        sell_review = sell_review_status(row, membership_signal, hold_status, price_status)
        rows.append({
            "bet_no": row["bet_no"],
            "ticker": row["ticker"],
            "symbol": symbol,
            "qty": row["qty"],
            "current_price": row["current_price"],
            "buy_price": row["buy_price"],
            "memberships": memberships,
            "highest_index": highest_index,
            "highest_index_label": display_index_name(highest_index),
            "other_memberships": other_memberships,
            "other_memberships_label": ", ".join(display_index_name(name) for name in other_memberships) if other_memberships else "-",
            "membership_signal": membership_signal,
            "hold_status": hold_status,
            "five_year_status": five_year_status,
            "price_status": price_status,
            "sell_review": sell_review,
        })

    # Keep mandatory review candidates at the top of the initial paginated view.
    # Python's stable sort preserves the existing order within each group.
    rows.sort(key=lambda item: item["sell_review"]["class"] != "mandatory")

    index_cards = [
        statuses_by_index.get(index_name) or {
            "name": index_name,
            "last": None,
            "change": None,
            "pct": None,
            "time": "-",
        }
        for index_name in tracked_indices
        if index_name in touched_indices
    ]
    index_by_name = {item["name"]: item for item in index_cards}
    market_context = []
    for index_name in ("NIFTY 50", "NIFTY 500"):
        if index_name in index_by_name:
            market_context.append(index_by_name[index_name])

    weak_indices = sorted(
        [
            item for item in index_cards
            if item["name"] not in {"NIFTY 50", "NIFTY 500"} and item.get("pct") is not None
        ],
        key=lambda item: item["pct"],
    )
    for item in weak_indices[:3]:
        if item not in market_context:
            market_context.append(item)

    for item in market_context:
        item["display_name"] = display_index_name(item["name"])

    above_avg_hold_count = len([
        row for row in rows
        if row["hold_status"]["class"] == "red"
    ])
    degraded_membership_count = len([
        row for row in rows
        if row["membership_signal"]["class"] in {"downgraded", "removed"}
    ])
    promoted_membership_count = len([
        row for row in rows
        if row["membership_signal"]["class"] == "promoted"
    ])
    mandatory_sell_review_count = len([
        row for row in rows
        if row["sell_review"]["class"] == "mandatory"
    ])
    deep_drawdown_count = len([
        row for row in rows
        if row["price_status"]["class"] == "red"
    ])
    five_year_hold_count = len([
        row for row in rows
        if (row["hold_status"].get("open_days") or 0) >= 365 * 5
    ])

    try:
        current_snapshot = {
            symbol: sort_memberships(indexes)
            for symbol, indexes in memberships_by_symbol.items()
        }
        has_broad_index_data = any(
            any(index_name in BROAD_INDEXES for index_name in indexes)
            for indexes in current_snapshot.values()
        )
        if current_snapshot and has_broad_index_data:
            save_snapshot(current_snapshot, snapshot_as_of)
        else:
            logger.warning(
                "Index membership snapshot not saved: symbols=%s broad_data=%s errors=%s",
                len(current_snapshot),
                has_broad_index_data,
                len(membership_errors),
            )
    except Exception:
        logger.exception("Could not save index membership snapshot")

    return {
        "rows": rows,
        "index_cards": index_cards,
        "market_context": market_context,
        "summary_cards": {
            "above_avg_hold_count": above_avg_hold_count,
            "degraded_membership_count": degraded_membership_count,
            "promoted_membership_count": promoted_membership_count,
            "mandatory_sell_review_count": mandatory_sell_review_count,
            "deep_drawdown_count": deep_drawdown_count,
            "five_year_hold_count": five_year_hold_count,
        },
        "tracked_index_count": len(tracked_indices),
        "snapshot_as_of": previous_snapshot.get("as_of"),
        "current_as_of": as_of,
        "membership_last_updated": _membership_cache.get("fetched_at"),
        "avg_winning_hold_days": avg_hold_days,
        "source": "NSE official index constituent CSV files",
        "source_available": source_available,
        "membership_errors": membership_errors,
        "status_errors": status_errors,
    }
