import csv
import io
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from services.date_utils import normalized_buy_date_map, parse_date, position_identity
from services.nse_service import fetch_bhavcopy_csv, normalize_nse_symbol, row_value
from services.yahoo_service import resolve_symbol


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
CACHE_PATH = BASE_DIR / "data" / "stock_profit_booking_simulation_cache.json"
TARGET_RATES = (0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.15, 0.20, 0.26)
CACHE_VERSION = "v3-ohlc"


def cached_history_fresh(entry, end_date):
    if end_date < date.today():
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(entry["fetched_at"])).total_seconds() < 300
    except (KeyError, ValueError):
        return False


def stop_loss_result(bars, buy_date, buy_price, qty, actual_value, loss_rate=0.15):
    """Fixed 15% stop overlay; no re-entry or invented returns on released cash."""
    stop = buy_price * (1 - loss_rate)
    eligible = sorted((bar for bar in bars if parse_date(bar["date"]) > buy_date), key=lambda bar: bar["date"])
    usable = bool(eligible) and all(
        bar.get("low") is not None and bar.get("open") is not None
        and math.isfinite(bar["low"]) and math.isfinite(bar["open"])
        for bar in eligible
    )
    hit = next((bar for bar in eligible if usable and bar["low"] <= stop), None)
    exit_price = min(stop, hit["open"]) if hit else None
    hit_day = parse_date(hit["date"]) if hit else None
    recovery = next((bar for bar in eligible if hit_day and parse_date(bar["date"]) > hit_day and bar["high"] >= buy_price), None)
    recovery_day = parse_date(recovery["date"]) if recovery else None
    value = (exit_price - buy_price) * qty if hit else (actual_value if usable else None)
    return {"stop_price": stop, "hit": bool(hit), "hit_date": hit["date"] if hit else None,
            "exit_price": exit_price, "pl": value, "delta": value - actual_value if value is not None and actual_value is not None else None,
            "available": usable, "actual_pl": actual_value, "recovered": bool(recovery),
            "recovery_date": recovery["date"] if recovery else None,
            "recovery_days": (recovery_day - hit_day).days if recovery_day and hit_day else None}


def include_purchase_day(result, position, buy_date, today):
    """Retain same-day open MTM; current observations are not execution evidence."""
    price = position["current_price"]
    if buy_date != today or is_closed_position(position) or price is None or not math.isfinite(price) or price <= 0:
        return result
    target = position["target_price"]
    status = ("At/below stop" if price <= result["stop_price"] else
              "At/above target" if target is not None and target > 0 and price >= target else
              "Between stop and target" if target is not None and target > 0 else
              "Above stop · no target set")
    return dict(result, available=True, hit=False, hit_date=None, exit_price=None,
                pl=actual_pl(position), delta=0., purchase_day=True,
                current_status=status, intraday_verified=False)


def load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read stock profit booking simulation cache: %s", CACHE_PATH)
        return {}


def stop_comparison(rows, positions, scenario_key="stop_loss"):
    rows = [dict(row, stop_loss=row[scenario_key]) for row in rows]
    def totals(items):
        result = dict(open_count=0, closed_count=0, realized_pl=0., unrealized_pl=0., open_capital=0., missing_values=0)
        for closed, value, capital in items:
            result["closed_count" if closed else "open_count"] += 1
            if not closed:
                result["open_capital"] += capital
            if value is None:
                result["missing_values"] += 1
            else:
                result["realized_pl" if closed else "unrealized_pl"] += value
        result["total_pl"] = result["realized_pl"] + result["unrealized_pl"]
        return result

    comparable = [row for row in rows if row["stop_loss"]["delta"] is not None]
    actual_all = totals((is_closed_position(p), actual_pl(p), (p["buy_price"] or 0) * (p["qty"] or 0)) for p in positions)
    actual = totals((r["actual_closed"], r["actual_pl"], r["capital"]) for r in comparable)
    simulated = totals((r["actual_closed"] or r["stop_loss"]["hit"], r["stop_loss"]["pl"], r["capital"]) for r in comparable)
    stopped = [r for r in comparable if r["stop_loss"]["hit"]]
    recovered = [r for r in stopped if r["stop_loss"].get("recovered")]
    return {"all_actual": actual_all, "actual": actual, "simulated": simulated,
            "stop_realized": sum(r["stop_loss"]["pl"] for r in stopped),
            "stopped_actual": sum(r["actual_pl"] for r in stopped),
            "stop_delta": sum(r["stop_loss"]["delta"] for r in stopped),
            "other_realized": sum(r["actual_pl"] for r in comparable if r["actual_closed"] and not r["stop_loss"]["hit"]),
            "released_cash": sum(r["stop_loss"]["exit_price"] * r["qty"] for r in stopped),
            "recovered": len(recovered),
            "not_recovered": len(stopped) - len(recovered),
            "recovery_rate": round(len(recovered) / len(stopped) * 100, 2) if stopped else None,
            "avg_recovery_days": round(sum(r["stop_loss"]["recovery_days"] for r in recovered) / len(recovered), 1) if recovered else None,
            "differences": {key: simulated[key] - actual[key] for key in actual},
            "covered": len(comparable), "excluded": len(positions) - len(comparable),
            "hits": sum(r["stop_loss"]["hit"] for r in comparable),
            "newly_closed": sum(r["stop_loss"]["hit"] and not r["actual_closed"] for r in comparable)}


def save_cache(cache):
    from services.json_cache import atomic_write_json, prune_superseded

    atomic_write_json(CACHE_PATH, prune_superseded(cache, CACHE_VERSION))


def cache_key(ticker, start_date, end_date):
    return f"{CACHE_VERSION}|{ticker.strip().upper()}|{start_date.isoformat()}|{end_date.isoformat()}"


def history_highs(ticker, start_date, end_date, cache=None, cache_changed=None):
    owns_cache = cache is None
    cache = load_cache() if owns_cache else cache
    key = cache_key(ticker, start_date, end_date)
    if key in cache and cached_history_fresh(cache[key], end_date):
        return list(cache[key].get("highs") or [])

    symbol = resolve_symbol(ticker)
    fetch_end = end_date + timedelta(days=1)
    logger.info(
        "Stock profit booking history fetch started: ticker=%s symbol=%s start=%s end=%s",
        ticker,
        symbol,
        start_date,
        end_date,
    )
    highs = []
    yahoo_failed = False
    try:
        history = yf.Ticker(symbol).history(
            start=start_date.isoformat(),
            end=fetch_end.isoformat(),
            interval="1d",
            auto_adjust=False,
            timeout=15,
        )
        if not history.empty and "High" in history:
            for timestamp, value in history["High"].dropna().items():
                day = timestamp.date()
                if start_date <= day <= end_date:
                    bar = history.loc[timestamp]
                    highs.append({"date": day.isoformat(), "high": round(float(value), 2),
                                  "low": float(bar["Low"]), "open": float(bar["Open"])})
    except Exception:
        yahoo_failed = True
        logger.warning(
            "Yahoo history failed; trying NSE bhavcopy fallback: ticker=%s start=%s end=%s",
            ticker,
            start_date,
            end_date,
            exc_info=True,
        )

    if not highs and not yahoo_failed:
        logger.warning(
            "Yahoo history returned no daily highs; trying NSE bhavcopy fallback: ticker=%s start=%s end=%s",
            ticker,
            start_date,
            end_date,
        )
        highs = nse_history_highs(ticker, start_date, end_date)

    cache[key] = {
        "ticker": ticker.strip().upper(),
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "fetched_at": datetime.now().replace(microsecond=0).isoformat(),
        "highs": highs,
    }
    if owns_cache:
        save_cache(cache)
    elif cache_changed is not None:
        cache_changed[0] = True
    logger.info(
        "Stock profit booking history fetch completed: ticker=%s rows=%s",
        ticker,
        len(highs),
    )
    return list(highs)


def nse_high_from_bhavcopy(ticker, day):
    symbol = normalize_nse_symbol(ticker)
    csv_text = fetch_bhavcopy_csv(day)
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row_symbol = (row_value(row, ["SYMBOL", "TckrSymb", "TCKRSYMB"]) or "").strip().upper()
        series = (row_value(row, ["SERIES", "SctySrs", "SCTYSRS"]) or "EQ").strip().upper()
        if row_symbol != symbol or series not in {"EQ", ""}:
            continue
        high_text = row_value(row, ["HIGH_PRICE", "HIGH", "HghPric", "HGHLTP", "HIGH_PRICE"])
        if high_text is None:
            continue
        high_price = float(str(high_text).replace(",", ""))
        return round(high_price, 2)
    raise ValueError(f"{symbol} high price not found in NSE bhavcopy for {day}")


def nse_history_highs(ticker, start_date, end_date):
    highs = []
    day = start_date
    while day <= end_date:
        if day.weekday() < 5:
            try:
                high = nse_high_from_bhavcopy(ticker, day)
                highs.append({"date": day.isoformat(), "high": high})
            except Exception:
                logger.debug("NSE high unavailable for simulation: ticker=%s date=%s", ticker, day)
        day += timedelta(days=1)
    return highs


def is_closed_position(row):
    status = (row["status"] or "").strip().lower()
    return row["sell_price"] is not None or row["sell_date"] is not None or status in {"closed", "target hit", "stop loss hit", "sold"}


def actual_pl(row):
    qty = row["qty"] or 0
    buy_price = row["buy_price"] or 0
    if is_closed_position(row) and row["sell_price"] is not None:
        return (row["sell_price"] - buy_price) * qty
    if row["current_price"] is not None:
        return (row["current_price"] - buy_price) * qty
    return None


def actual_pl_basis(row):
    if is_closed_position(row) and row["sell_price"] is not None:
        return "Closed realized"
    if row["current_price"] is not None:
        return "Open MTM"
    return "Unavailable"


def first_hit(highs, target_price):
    for item in highs:
        if item["high"] >= target_price:
            return item
    return None


def fmt_rate(rate):
    return f"{int(round(rate * 100))}%"


def confidence_label(total_bets, missing_history):
    if not total_bets:
        return "No data"
    usable = total_bets - missing_history
    if usable >= 100:
        return "Strong"
    if usable >= 50:
        return "Building"
    return "Early"


def early_exit_candidate(position):
    return (
        is_closed_position(position)
        and position["sell_price"] is not None
        and position["sell_date"] is not None
        and position["target_price"] is not None
        and position["buy_price"] not in (None, 0)
        and position["qty"]
        and position["sell_price"] < position["target_price"]
    )


def build_early_exit_result(position, buy_date, sell_date, today, actual_value, history_cache=None, cache_changed=None):
    qty = position["qty"] or 0
    buy_price = position["buy_price"] or 0
    target_price = position["target_price"]
    sell_price = position["sell_price"]
    original_capital = buy_price * qty
    released_capital = sell_price * qty
    target_pl = (target_price - buy_price) * qty
    missed_pl = target_pl - (actual_value or 0)
    search_end = max(today, sell_date)
    try:
        post_sell_highs = history_highs(position["ticker"], sell_date, search_end, history_cache, cache_changed)
        hit = first_hit(post_sell_highs, target_price)
        history_error = None
    except Exception as exc:
        logger.exception(
            "Early exit target follow-up history unavailable: bet_no=%s ticker=%s",
            position["bet_no"],
            position["ticker"],
        )
        hit = None
        history_error = str(exc)

    hit_date = parse_date(hit["date"]) if hit else None
    days_after_sell = (hit_date - sell_date).days if hit_date else None
    hold_days_to_target = (hit_date - buy_date).days if hit_date else None
    return {
        "bet_no": position["bet_no"],
        "ticker": position["ticker"],
        "buy_date": buy_date,
        "sell_date": sell_date,
        "qty": qty,
        "buy_price": buy_price,
        "original_capital": original_capital,
        "released_capital": released_capital,
        "sell_price": sell_price,
        "target_price": target_price,
        "actual_pl": actual_value,
        "target_pl": target_pl,
        "missed_pl": missed_pl,
        "missed_return_on_released_pct": (
            missed_pl / released_capital * 100
            if released_capital
            else None
        ),
        "target_hit_after_sell": bool(hit),
        "target_hit_date": hit_date,
        "days_after_sell": days_after_sell,
        "hold_days_to_target": hold_days_to_target,
        "capital_days_released": (
            released_capital * days_after_sell
            if days_after_sell is not None
            else None
        ),
        "highest_after_sell": hit["high"] if hit else None,
        "history_error": history_error,
    }


def simulate_stock_profit_booking(positions, today=None):
    today = today or date.today()
    history_cache = load_cache()
    cache_changed = [False]
    normalized_buy_dates = normalized_buy_date_map(positions, today=today)
    # Warm independent network requests concurrently, then merge/write on this thread.
    requests = {}
    for position in positions:
        start = normalized_buy_dates.get(position_identity(position)) or parse_date(position["buy_date"])
        if not start or not position["buy_price"] or not position["qty"]:
            continue
        end = parse_date(position["sell_date"]) if is_closed_position(position) else None
        end = max(start, end or today)
        key = cache_key(position["ticker"], start, end)
        if key not in history_cache or not cached_history_fresh(history_cache[key], end):
            requests[key] = (position["ticker"], start, end)
    def prefetch(item):
        local_cache = {}
        try:
            history_highs(*item, cache=local_cache, cache_changed=[False])
        except Exception:
            logger.exception("Simulation history prefetch failed: %s", item[0])
        return local_cache
    if requests:
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="simulation-history") as pool:
            for fetched in pool.map(prefetch, requests.values()):
                history_cache.update(fetched)
                cache_changed[0] = cache_changed[0] or bool(fetched)
    rows = []
    summary = {
        "bets": 0,
        "capital": 0,
        "realized_pl": 0,
        "realized_count": 0,
        "realized_capital": 0,
        "winning_realized_count": 0,
        "winning_realized_capital": 0,
        "winning_hold_days": [],
        "open_mtm": 0,
        "open_mtm_count": 0,
        "open_target_expected_pl": 0,
        "open_target_expected_count": 0,
        "open_target_capital": 0,
        "open_target_missing_count": 0,
        "current_total_pl": 0,
        "actual_pl_available": 0,
        "missing_history": 0,
        "early_exit_count": 0,
        "early_exit_target_hits": 0,
        "early_exit_pending": 0,
        "early_exit_missed_pl": 0,
        "early_exit_original_capital": 0,
        "early_exit_released_capital": 0,
        "early_exit_hit_released_capital": 0,
        "early_exit_capital_days_released": 0,
        "early_exit_days_after_sell": [],
        "early_exit_rows": [],
        "targets": {
            rate: {
                "label": fmt_rate(rate),
                "hits": 0,
                "booked_profit": 0,
                "open_expected_pl": 0,
                "open_expected_count": 0,
                "open_expected_capital": 0,
                "total_with_unhit_mtm": 0,
                "missed_or_saved_vs_realized": 0,
                "missed_or_saved_vs_current": 0,
                "hit_days": [],
                "capital_hit": 0,
                "capital_released": 0,
                "redeploy_days": [],
                "capital_days_released": 0,
            }
            for rate in TARGET_RATES
        },
    }

    for position in positions:
        buy_date = normalized_buy_dates.get(position_identity(position)) or parse_date(position["buy_date"])
        if not buy_date or position["buy_price"] in (None, 0) or not position["qty"]:
            continue

        sell_date = parse_date(position["sell_date"])
        end_date = sell_date if is_closed_position(position) and sell_date else today
        if end_date < buy_date:
            end_date = buy_date

        qty = position["qty"] or 0
        buy_price = position["buy_price"] or 0
        capital = qty * buy_price
        actual_value = actual_pl(position)
        actual_basis = actual_pl_basis(position)
        try:
            highs = history_highs(position["ticker"], buy_date, end_date, history_cache, cache_changed)
            stop_result = stop_loss_result(highs, buy_date, buy_price, qty, actual_value)
            stop_75 = stop_loss_result(highs, buy_date, buy_price, qty, actual_value, 0.75)
            if end_date == today and position["current_price"] is not None:
                highs.append({"date": today.isoformat(), "high": round(float(position["current_price"]), 2)})
            history_error = None
        except Exception as exc:
            logger.exception(
                "Stock profit booking simulation history unavailable: bet_no=%s ticker=%s",
                position["bet_no"],
                position["ticker"],
            )
            highs = []
            history_error = str(exc)
            stop_result = stop_loss_result([], buy_date, buy_price, qty, actual_value)
            stop_75 = stop_loss_result([], buy_date, buy_price, qty, actual_value, 0.75)
            summary["missing_history"] += 1

        stop_result = include_purchase_day(stop_result, position, buy_date, today)
        stop_75 = include_purchase_day(stop_75, position, buy_date, today)
        result = {
            "position_id": position["id"],
            "bet_no": position["bet_no"],
            "ticker": position["ticker"],
            "buy_date": buy_date,
            "end_date": end_date,
            "status": position["status"] or "Open",
            "qty": qty,
            "buy_price": buy_price,
            "current_price": position["current_price"],
            "target_price": position["target_price"],
            "capital": capital,
            "actual_pl": actual_value,
            "actual_basis": actual_basis,
            "actual_closed": is_closed_position(position),
            "history_error": history_error,
            "early_exit": None,
            "targets": {},
            "stop_loss": stop_result,
            "stop_75": stop_75,
        }

        summary["bets"] += 1
        summary["capital"] += capital
        if actual_value is not None:
            summary["current_total_pl"] += actual_value
            summary["actual_pl_available"] += 1
            if actual_basis == "Closed realized":
                summary["realized_pl"] += actual_value
                summary["realized_count"] += 1
                summary["realized_capital"] += capital
                if actual_value > 0:
                    summary["winning_realized_count"] += 1
                    summary["winning_realized_capital"] += capital
                    if sell_date:
                        summary["winning_hold_days"].append((sell_date - buy_date).days)
            elif actual_basis == "Open MTM":
                summary["open_mtm"] += actual_value
                summary["open_mtm_count"] += 1

        if not is_closed_position(position):
            target_price = position["target_price"]
            if target_price is None:
                summary["open_target_missing_count"] += 1
            else:
                summary["open_target_expected_count"] += 1
                summary["open_target_capital"] += capital
                summary["open_target_expected_pl"] += (target_price - buy_price) * qty

        if early_exit_candidate(position):
            early_exit = build_early_exit_result(position, buy_date, sell_date, today, actual_value, history_cache, cache_changed)
            result["early_exit"] = early_exit
            summary["early_exit_count"] += 1
            summary["early_exit_rows"].append(early_exit)
            summary["early_exit_original_capital"] += early_exit["original_capital"]
            summary["early_exit_released_capital"] += early_exit["released_capital"]
            if early_exit["target_hit_after_sell"]:
                summary["early_exit_target_hits"] += 1
                summary["early_exit_missed_pl"] += early_exit["missed_pl"]
                summary["early_exit_hit_released_capital"] += early_exit["released_capital"]
                summary["early_exit_capital_days_released"] += early_exit["capital_days_released"] or 0
                if early_exit["days_after_sell"] is not None:
                    summary["early_exit_days_after_sell"].append(early_exit["days_after_sell"])
            else:
                summary["early_exit_pending"] += 1

        for rate in TARGET_RATES:
            target_price = buy_price * (1 + rate)
            hit = first_hit(highs, target_price)
            booked_profit = capital * rate if hit else 0
            unhit_mtm = 0 if hit else (actual_value or 0)
            total_with_unhit = booked_profit + unhit_mtm
            target_summary = summary["targets"][rate]
            if hit:
                target_summary["hits"] += 1
            target_summary["booked_profit"] += booked_profit
            if not is_closed_position(position):
                target_summary["open_expected_count"] += 1
                target_summary["open_expected_capital"] += capital
                target_summary["open_expected_pl"] += capital * rate
            target_summary["total_with_unhit_mtm"] += total_with_unhit
            target_summary["missed_or_saved_vs_realized"] += total_with_unhit - (actual_value if actual_basis == "Closed realized" else 0)
            if actual_value is not None:
                target_summary["missed_or_saved_vs_current"] += total_with_unhit - actual_value
            if hit:
                hit_date = parse_date(hit["date"])
                target_summary["capital_hit"] += capital
                released_capital = target_price * qty
                target_summary["capital_released"] += released_capital
                if hit_date:
                    target_summary["hit_days"].append((hit_date - buy_date).days)
                    redeploy_days = max((today - hit_date).days, 0)
                    target_summary["redeploy_days"].append(redeploy_days)
                    target_summary["capital_days_released"] += released_capital * redeploy_days

            result["targets"][rate] = {
                "label": fmt_rate(rate),
                "target_price": target_price,
                "hit": bool(hit),
                "hit_date": parse_date(hit["date"]) if hit else None,
                "hit_high": hit["high"] if hit else None,
                "booked_profit": booked_profit,
                "released_capital": target_price * qty if hit else 0,
                "redeploy_days": max((today - parse_date(hit["date"])).days, 0) if hit and parse_date(hit["date"]) else None,
                "unhit_mtm": unhit_mtm,
                "total_with_unhit_mtm": total_with_unhit,
                "vs_realized": total_with_unhit - (actual_value if actual_basis == "Closed realized" else 0),
                "vs_current": (total_with_unhit - actual_value) if actual_value is not None else None,
            }

        rows.append(result)

    for rate, target_summary in summary["targets"].items():
        target_summary["hit_rate"] = (target_summary["hits"] / summary["bets"] * 100) if summary["bets"] else None
        target_summary["avg_booked_profit"] = (
            target_summary["booked_profit"] / target_summary["hits"]
            if target_summary["hits"]
            else None
        )
        target_summary["avg_days_to_hit"] = (
            sum(target_summary["hit_days"]) / len(target_summary["hit_days"])
            if target_summary["hit_days"]
            else None
        )
        target_summary["fastest_days_to_hit"] = min(target_summary["hit_days"]) if target_summary["hit_days"] else None
        target_summary["slowest_days_to_hit"] = max(target_summary["hit_days"]) if target_summary["hit_days"] else None
        target_summary["capital_hit_rate"] = (
            target_summary["capital_hit"] / summary["capital"] * 100
            if summary["capital"]
            else None
        )
        target_summary["missed_bets"] = summary["bets"] - target_summary["hits"]
        target_summary["profit_per_analysed_bet"] = (
            target_summary["booked_profit"] / summary["bets"]
            if summary["bets"]
            else None
        )
        target_summary["avg_released_capital"] = (
            target_summary["capital_released"] / target_summary["hits"]
            if target_summary["hits"]
            else None
        )
        target_summary["avg_redeploy_days"] = (
            sum(target_summary["redeploy_days"]) / len(target_summary["redeploy_days"])
            if target_summary["redeploy_days"]
            else None
        )
        target_summary["capital_released_rate"] = (
            target_summary["capital_released"] / summary["capital"] * 100
            if summary["capital"]
            else None
        )
        target_summary["capital_days_per_lakh"] = (
            target_summary["capital_days_released"] / 100000
            if target_summary["capital_days_released"]
            else None
        )
        target_summary["open_expected_return"] = (
            target_summary["open_expected_pl"] / target_summary["open_expected_capital"] * 100
            if target_summary["open_expected_capital"]
            else None
        )
        del target_summary["hit_days"]
        del target_summary["redeploy_days"]

    summary["actual_win_rate"] = (
        summary["winning_realized_count"] / summary["bets"] * 100
        if summary["bets"]
        else None
    )
    summary["actual_avg_realized_profit"] = (
        summary["realized_pl"] / summary["realized_count"]
        if summary["realized_count"]
        else None
    )
    summary["actual_avg_winning_hold_days"] = (
        sum(summary["winning_hold_days"]) / len(summary["winning_hold_days"])
        if summary["winning_hold_days"]
        else None
    )
    summary["actual_winning_capital_rate"] = (
        summary["winning_realized_capital"] / summary["capital"] * 100
        if summary["capital"]
        else None
    )
    summary["open_target_expected_return"] = (
        summary["open_target_expected_pl"] / summary["open_target_capital"] * 100
        if summary["open_target_capital"]
        else None
    )
    summary["early_exit_target_hit_rate"] = (
        summary["early_exit_target_hits"] / summary["early_exit_count"] * 100
        if summary["early_exit_count"]
        else None
    )
    summary["early_exit_avg_days_after_sell"] = (
        sum(summary["early_exit_days_after_sell"]) / len(summary["early_exit_days_after_sell"])
        if summary["early_exit_days_after_sell"]
        else None
    )
    summary["early_exit_avg_released_capital"] = (
        summary["early_exit_released_capital"] / summary["early_exit_count"]
        if summary["early_exit_count"]
        else None
    )
    summary["early_exit_missed_return_on_released_pct"] = (
        summary["early_exit_missed_pl"] / summary["early_exit_hit_released_capital"] * 100
        if summary["early_exit_hit_released_capital"]
        else None
    )
    summary["early_exit_missed_pl_per_lakh_released"] = (
        summary["early_exit_missed_pl"] / summary["early_exit_hit_released_capital"] * 100000
        if summary["early_exit_hit_released_capital"]
        else None
    )
    summary["early_exit_rows"].sort(
        key=lambda row: (
            0 if row["target_hit_after_sell"] else 1,
            row["days_after_sell"] if row["days_after_sell"] is not None else 999999,
            row["sell_date"],
            row["ticker"],
        )
    )
    del summary["winning_hold_days"]
    del summary["early_exit_days_after_sell"]

    ranked_by_profit = sorted(
        summary["targets"].items(),
        key=lambda item: item[1]["booked_profit"],
        reverse=True,
    )
    highest_profit_rate, highest_profit_target = ranked_by_profit[0] if ranked_by_profit else (None, None)
    best_by_hit_rate = max(
        summary["targets"].items(),
        key=lambda item: item[1]["hit_rate"] if item[1]["hit_rate"] is not None else -1,
        default=(None, None),
    )
    fastest_target = min(
        (
            (rate, target)
            for rate, target in summary["targets"].items()
            if target["avg_days_to_hit"] is not None
        ),
        key=lambda item: item[1]["avg_days_to_hit"],
        default=(None, None),
    )
    best_capital_target = max(
        summary["targets"].items(),
        key=lambda item: item[1]["capital_hit_rate"] if item[1]["capital_hit_rate"] is not None else -1,
        default=(None, None),
    )
    max_booked_profit = max(
        (target["booked_profit"] for target in summary["targets"].values()),
        default=0,
    )
    max_hit_rate = max(
        (target["hit_rate"] or 0 for target in summary["targets"].values()),
        default=0,
    )
    max_capital_hit_rate = max(
        (target["capital_hit_rate"] or 0 for target in summary["targets"].values()),
        default=0,
    )
    hit_day_values = [
        target["avg_days_to_hit"]
        for target in summary["targets"].values()
        if target["avg_days_to_hit"] is not None
    ]
    min_hit_days = min(hit_day_values) if hit_day_values else None
    max_hit_days = max(hit_day_values) if hit_day_values else None
    for target in summary["targets"].values():
        profit_score = (
            target["booked_profit"] / max_booked_profit * 100
            if max_booked_profit
            else 0
        )
        hit_score = target["hit_rate"] or 0
        if target["avg_days_to_hit"] is None:
            speed_score = 0
        elif min_hit_days == max_hit_days:
            speed_score = 100
        else:
            speed_score = (max_hit_days - target["avg_days_to_hit"]) / (max_hit_days - min_hit_days) * 100
        capital_score = target["capital_hit_rate"] or 0
        target["profit_score"] = profit_score
        target["hit_score"] = hit_score
        target["speed_score"] = speed_score
        target["capital_score"] = capital_score
        target["decision_score"] = (
            profit_score * 0.40
            + hit_score * 0.30
            + speed_score * 0.20
            + capital_score * 0.10
        )

    ranked_by_decision = sorted(
        summary["targets"].items(),
        key=lambda item: item[1]["decision_score"],
        reverse=True,
    )
    recommended_rate, recommended_target = ranked_by_decision[0] if ranked_by_decision else (None, None)
    second_recommended = ranked_by_decision[1][1] if len(ranked_by_decision) > 1 else None
    summary["best_rate"] = recommended_rate
    summary["best_target"] = recommended_target
    summary["highest_profit_rate"] = highest_profit_rate
    summary["highest_profit_target"] = highest_profit_target
    if recommended_target:
        recommended_target["is_best"] = True
        recommended_target["beats_realized_by"] = recommended_target["booked_profit"] - summary["realized_pl"]
        recommended_target["insight"] = (
            f"{recommended_target['label']} has the best balanced score across profit, hit rate, speed, and capital coverage."
        )
    if highest_profit_target:
        highest_profit_target["is_profit_best"] = True
        highest_profit_target["beats_realized_by"] = highest_profit_target["booked_profit"] - summary["realized_pl"]

    summary["analytics"] = {
        "confidence": confidence_label(summary["bets"], summary["missing_history"]),
        "sample_text": f"{summary['bets']} analysed bets; {summary['missing_history']} missing history",
        "best_label": recommended_target["label"] if recommended_target else "-",
        "best_profit": recommended_target["booked_profit"] if recommended_target else None,
        "best_score": recommended_target["decision_score"] if recommended_target else None,
        "best_gap": (
            recommended_target["decision_score"] - second_recommended["decision_score"]
            if recommended_target and second_recommended
            else None
        ),
        "best_gap_label": second_recommended["label"] if second_recommended else None,
        "highest_profit_label": highest_profit_target["label"] if highest_profit_target else "-",
        "highest_profit": highest_profit_target["booked_profit"] if highest_profit_target else None,
        "best_by_hit_rate_label": best_by_hit_rate[1]["label"] if best_by_hit_rate[1] else "-",
        "best_by_hit_rate": best_by_hit_rate[1]["hit_rate"] if best_by_hit_rate[1] else None,
        "fastest_label": fastest_target[1]["label"] if fastest_target[1] else "-",
        "fastest_days": fastest_target[1]["avg_days_to_hit"] if fastest_target[1] else None,
        "capital_label": best_capital_target[1]["label"] if best_capital_target[1] else "-",
        "capital_hit_rate": best_capital_target[1]["capital_hit_rate"] if best_capital_target[1] else None,
        "score_formula": "Score = 40% profit + 30% hit rate + 20% speed + 10% capital coverage",
    }
    summary["analytics"]["takeaways"] = []
    if recommended_target:
        summary["analytics"]["takeaways"].append(
            f"{recommended_target['label']} is recommended because it balances profit, hit rate, speed, and capital coverage."
        )
    if highest_profit_target and recommended_target and highest_profit_target is not recommended_target:
        summary["analytics"]["takeaways"].append(
            f"{highest_profit_target['label']} has the highest booked profit, but the recommendation also considers how often and how quickly targets hit."
        )
    if fastest_target[1]:
        summary["analytics"]["takeaways"].append(
            f"{fastest_target[1]['label']} reaches targets fastest on average."
        )
    if summary["early_exit_count"]:
        if summary["early_exit_target_hits"]:
            summary["analytics"]["takeaways"].append(
                f"{summary['early_exit_target_hits']} of {summary['early_exit_count']} early exits later reached the original target."
            )
        else:
            summary["analytics"]["takeaways"].append(
                f"{summary['early_exit_count']} closed-under-target exits are being tracked for target follow-through."
            )
    if summary["analytics"]["confidence"] != "Strong":
        summary["analytics"]["takeaways"].append(
            "Treat this as directional until the sample reaches about 100 stock bets."
        )
    summary["scenario_rankings"] = []
    for rank, (rate, target) in enumerate(ranked_by_decision, start=1):
        notes = []
        if target.get("is_best"):
            notes.append("Recommended")
        if target.get("is_profit_best"):
            notes.append("Highest profit")
        if fastest_target[0] == rate:
            notes.append("Fastest")
        if best_by_hit_rate[0] == rate:
            notes.append("Highest hit rate")
        if best_capital_target[0] == rate:
            notes.append("Most capital covered")
        if not notes:
            notes.append("Middle of the pack")
        summary["scenario_rankings"].append(
            {
                "rank": rank,
                "label": target["label"],
                "expected_total_pl": target["total_with_unhit_mtm"],
                "expected_vs_realized": target["total_with_unhit_mtm"] - summary["realized_pl"],
                "open_expected_pl": target["open_expected_pl"],
                "open_expected_count": target["open_expected_count"],
                "open_expected_return": target["open_expected_return"],
                "booked_profit": target["booked_profit"],
                "hits": target["hits"],
                "hit_rate": target["hit_rate"],
                "avg_days_to_hit": target["avg_days_to_hit"],
                "capital_hit_rate": target["capital_hit_rate"],
                "capital_released": target["capital_released"],
                "avg_redeploy_days": target["avg_redeploy_days"],
                "capital_days_per_lakh": target["capital_days_per_lakh"],
                "profit_per_analysed_bet": target["profit_per_analysed_bet"],
                "decision_score": target["decision_score"],
                "is_best": target.get("is_best", False),
                "is_profit_best": target.get("is_profit_best", False),
                "note": " / ".join(notes),
            }
        )

    rows.sort(key=lambda row: (row["buy_date"], row["bet_no"] or 0), reverse=True)
    summary["stop_loss"] = stop_comparison(rows, positions)
    summary["stop_75"] = stop_comparison(rows, positions, "stop_75")
    if cache_changed[0]:
        save_cache(history_cache)
    return {"rows": rows, "summary": summary, "target_rates": TARGET_RATES}
