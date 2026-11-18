"""Capital-constrained, day-by-day stock portfolio replay.

Unlike the target-hit report, this module reuses sale proceeds only after an
actual simulated exit and skips a historical buy when cash is unavailable.
"""

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from services.date_utils import parse_date
from services.yahoo_service import resolve_symbol


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
CACHE_PATH = BASE_DIR / "data" / "historical_portfolio_simulator_cache.json"
STARTING_CAPITAL = 1_000_000.0
TARGETS = ("actual", None, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.26)
STOP_LOSS_FACTOR = 0.25
MAX_HOLD_DAYS = 365 * 5


def _load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read historical portfolio simulator cache", exc_info=True)
        return {}


def _save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _cache_key(ticker, start_date, end_date):
    return f"ohlc-v1|{ticker.strip().upper()}|{start_date.isoformat()}|{end_date.isoformat()}"


def daily_ohlc(ticker, start_date, end_date, cache=None, cache_changed=None):
    """Fetch daily raw OHLC bars, caching each bet's required history."""
    owns_cache = cache is None
    cache = _load_cache() if owns_cache else cache
    key = _cache_key(ticker, start_date, end_date)
    if key in cache:
        return cache[key].get("bars", [])

    bars = []
    symbol = resolve_symbol(ticker)
    try:
        history = yf.Ticker(symbol).history(
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if not history.empty:
            for timestamp, row in history.iterrows():
                day = timestamp.date()
                if not start_date <= day <= end_date:
                    continue
                high, low, close = (row.get("High"), row.get("Low"), row.get("Close"))
                if any(value is None for value in (high, low, close)):
                    continue
                bars.append(
                    {
                        "date": day.isoformat(),
                        "high": round(float(high), 2),
                        "low": round(float(low), 2),
                        "close": round(float(close), 2),
                    }
                )
    except Exception:
        logger.warning("Daily OHLC unavailable: ticker=%s symbol=%s", ticker, symbol, exc_info=True)

    cache[key] = {
        "ticker": ticker.strip().upper(),
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "fetched_at": datetime.now().replace(microsecond=0).isoformat(),
        "bars": bars,
    }
    if owns_cache:
        _save_cache(cache)
    elif cache_changed is not None:
        cache_changed[0] = True
    return bars


def _as_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _position_records(positions, history_provider, as_of_date):
    cache = _load_cache()
    cache_changed = [False]
    records, missing_history = [], []
    for row in positions:
        buy_date = parse_date(row["buy_date"])
        qty, entry = _as_float(row["qty"]), _as_float(row["buy_price"])
        if not buy_date or qty <= 0 or entry <= 0:
            continue
        bars = history_provider(row["ticker"], buy_date, as_of_date, cache, cache_changed)
        if not bars:
            missing_history.append(str(row["ticker"] or "Unknown"))
        records.append(
            {
                "id": row["id"],
                "bet_no": row["bet_no"],
                "ticker": row["ticker"],
                "buy_date": buy_date,
                "qty": qty,
                "entry": entry,
                "capital": round(qty * entry, 2),
                "actual_sell_date": parse_date(row["sell_date"]),
                "actual_sell_price": _as_float(row["sell_price"]) or None,
                "bars": {parse_date(item["date"]): item for item in bars if parse_date(item["date"])},
            }
        )
    if cache_changed[0]:
        _save_cache(cache)
    return records, missing_history


def _cagr(start_value, end_value, first_day, last_day):
    if start_value <= 0 or end_value <= 0 or not first_day or not last_day:
        return None
    years = max((last_day - first_day).days / 365.25, 1 / 365.25)
    return ((end_value / start_value) ** (1 / years) - 1) * 100


def _risk_metrics(values):
    if len(values) < 3:
        return None, None
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values)) if values[index - 1] > 0]
    if len(returns) < 2:
        return None, None
    average = sum(returns) / len(returns)
    variance = sum((item - average) ** 2 for item in returns) / (len(returns) - 1)
    sharpe = average / math.sqrt(variance) * math.sqrt(252) if variance > 0 else None
    downside = [min(0.0, item) for item in returns]
    downside_variance = sum(item ** 2 for item in downside) / len(downside)
    sortino = average / math.sqrt(downside_variance) * math.sqrt(252) if downside_variance > 0 else None
    return sharpe, sortino


def _run_scenario(records, target_rate, starting_capital=STARTING_CAPITAL, as_of_date=None, include_detail=False):
    as_of_date = as_of_date or date.today()
    records_by_buy_date = defaultdict(list)
    all_days = set()
    for record in records:
        records_by_buy_date[record["buy_date"]].append(record)
        all_days.add(record["buy_date"])
        all_days.update(record["bars"].keys())
    days = sorted(day for day in all_days if day <= as_of_date)
    if not days:
        return None

    cash = starting_capital
    open_positions, values, holdings = [], [], []
    taken = skipped = stop_exits = five_year_exits = target_exits = ambiguous_days = 0
    cash_values, utilization_values, underwater_values = [], [], []
    exit_holds = []
    year_start_values, yearly_returns = {}, {}
    ledger = []
    equity_curve = []

    def portfolio_value(exclude_position=None):
        market_value = sum(
            position["qty"] * position.get("last_close", position["entry"])
            for position in open_positions
            if position is not exclude_position
        )
        return cash + market_value

    def record_ledger(event, position, cash_before, capital_used=None, sale_proceeds=None, exclude_position=None):
        if not include_detail:
            return
        ledger.append(
            {
                "date": day.isoformat(),
                "event": event,
                "ticker": position["ticker"],
                "bet_no": position.get("bet_no"),
                "cash_before": round(cash_before, 2),
                "capital_used": round(capital_used, 2) if capital_used is not None else None,
                "sale_proceeds": round(sale_proceeds, 2) if sale_proceeds is not None else None,
                "cash_after": round(cash, 2),
                "portfolio_value": round(portfolio_value(exclude_position), 2),
            }
        )

    for day in days:
        # Exit first: cash is available to the day's incoming bets.
        survivors = []
        for position in open_positions:
            bar = position["bars"].get(day)
            if bar:
                position["last_close"] = bar["close"]
            hold_days = (day - position["buy_date"]).days
            exit_price = exit_reason = None
            actual_exit = (
                target_rate == "actual"
                and position.get("actual_sell_date")
                and day >= position["actual_sell_date"]
                and position.get("actual_sell_price") is not None
            )
            stop_hit = bool(bar and bar["low"] <= position["entry"] * STOP_LOSS_FACTOR)
            target_hit = bool(
                isinstance(target_rate, (int, float))
                and bar
                and bar["high"] >= position["entry"] * (1 + target_rate)
            )
            if actual_exit:
                exit_price, exit_reason = position["actual_sell_price"], "Actual recorded exit"
            elif stop_hit and target_hit:
                # Daily OHLC cannot order the two intraday events. Use the loss,
                # which is conservative, and count it for transparent reporting.
                exit_price, exit_reason = position["entry"] * STOP_LOSS_FACTOR, "Ambiguous day — conservative −75% exit"
                ambiguous_days += 1
                stop_exits += 1
            elif stop_hit:
                exit_price, exit_reason = position["entry"] * STOP_LOSS_FACTOR, "−75% exit"
                stop_exits += 1
            elif hold_days >= MAX_HOLD_DAYS:
                exit_price = (bar or {}).get("close") or position.get("last_close") or position["entry"]
                exit_reason = "5-year exit"
                five_year_exits += 1
            elif target_hit:
                exit_price, exit_reason = position["entry"] * (1 + target_rate), "Profit target"
                target_exits += 1

            if exit_price is None:
                survivors.append(position)
                continue
            cash_before = cash
            cash += position["qty"] * exit_price
            exit_holds.append(hold_days)
            record_ledger(
                exit_reason,
                position,
                cash_before,
                sale_proceeds=position["qty"] * exit_price,
                exclude_position=position,
            )
        open_positions = survivors

        # Then enter today's historical qualifying bets, in bet-number order.
        for record in sorted(records_by_buy_date.get(day, []), key=lambda item: (item["bet_no"] or 0, item["id"] or 0)):
            if record["capital"] > cash + 0.01:
                skipped += 1
                record_ledger("Skipped — insufficient cash", record, cash, capital_used=record["capital"])
                continue
            cash_before = cash
            cash -= record["capital"]
            position = {**record, "last_close": record["entry"]}
            open_positions.append(position)
            taken += 1
            record_ledger("Buy", position, cash_before, capital_used=record["capital"])

        market_value = sum(position["qty"] * position.get("last_close", position["entry"]) for position in open_positions)
        value = cash + market_value
        values.append((day, value))
        cash_values.append(cash)
        utilization_values.append((market_value / value * 100) if value > 0 else 0)
        underwater_value = sum(
            position["qty"] * position.get("last_close", position["entry"])
            for position in open_positions
            if position.get("last_close", position["entry"]) <= position["entry"] * 0.8
        )
        underwater_values.append((underwater_value / value * 100) if value > 0 else 0)
        year_start_values.setdefault(day.year, value)
        yearly_returns[day.year] = (value / year_start_values[day.year] - 1) * 100 if year_start_values[day.year] else 0
        if include_detail:
            equity_curve.append(
                {"date": day.isoformat(), "value": round(value, 2), "cash": round(cash, 2)}
            )

    running_peak = 0.0
    max_drawdown = 0.0
    for _, value in values:
        running_peak = max(running_peak, value)
        if running_peak:
            max_drawdown = min(max_drawdown, (value / running_peak - 1) * 100)
    sharpe, sortino = _risk_metrics([value for _, value in values])
    final_day, final_value = values[-1]
    final_locked = sum(
        position["qty"] * position.get("last_close", position["entry"])
        for position in open_positions
        if position.get("last_close", position["entry"]) <= position["entry"] * 0.8
    )
    return {
        "label": "Actual" if target_rate == "actual" else "No target" if target_rate is None else f"{target_rate * 100:.0f}%",
        "target_rate": target_rate,
        "final_value": round(final_value, 2),
        "cagr": round(_cagr(starting_capital, final_value, days[0], final_day), 2),
        "max_drawdown": round(max_drawdown, 2),
        "bets_taken": taken,
        "bets_skipped": skipped,
        "avg_hold_days": round(sum(exit_holds) / len(exit_holds), 1) if exit_holds else None,
        "stop_exits": stop_exits,
        "five_year_exits": five_year_exits,
        "target_exits": target_exits,
        "capital_locked": round(final_locked, 2),
        "locked_pct": round((final_locked / final_value * 100) if final_value else 0, 2),
        "average_cash": round(sum(cash_values) / len(cash_values), 2),
        "average_cash_pct": round((sum(cash_values) / len(cash_values) / starting_capital * 100) if starting_capital else 0, 2),
        "capital_utilization": round(sum(utilization_values) / len(utilization_values), 2),
        "underwater_over_20_pct": round(sum(underwater_values) / len(underwater_values), 2),
        "worst_year": round(min(yearly_returns.values()), 2) if yearly_returns else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "ambiguous_days": ambiguous_days,
        "open_positions": len(open_positions),
        "ledger": ledger if include_detail else None,
        "equity_curve": equity_curve if include_detail else None,
    }


def simulate_historical_portfolio(
    positions,
    starting_capital=STARTING_CAPITAL,
    as_of_date=None,
    history_provider=daily_ohlc,
    detail_target=0.07,
):
    as_of_date = as_of_date or date.today()
    records, missing_history = _position_records(positions, history_provider, as_of_date)
    scenarios = [
        _run_scenario(records, target, starting_capital=starting_capital, as_of_date=as_of_date)
        for target in TARGETS
    ]
    scenarios = [scenario for scenario in scenarios if scenario]
    target_scenarios = [
        scenario for scenario in scenarios
        if isinstance(scenario["target_rate"], (int, float))
    ]
    overview = {
        "best_final": max(target_scenarios, key=lambda item: item["final_value"], default=None),
        "best_cagr": max(target_scenarios, key=lambda item: item["cagr"] or float("-inf"), default=None),
        "lowest_drawdown": max(target_scenarios, key=lambda item: item["max_drawdown"], default=None),
        "control": next((scenario for scenario in scenarios if scenario["target_rate"] is None), None),
    }
    detail = _run_scenario(
        records,
        detail_target,
        starting_capital=starting_capital,
        as_of_date=as_of_date,
        include_detail=True,
    )
    return {
        "starting_capital": starting_capital,
        "as_of_date": as_of_date.isoformat(),
        "records_analysed": len(records),
        "missing_history": sorted(set(missing_history)),
        "scenarios": scenarios,
        "overview": overview,
        "detail": detail,
        "rules_note": "Target uses daily High; −75% uses daily Low; valuation uses daily Close. Same-day target and −75% touches use the conservative −75% exit. Historical index-removal events are not available and are not simulated.",
    }


def _run_funding_scenario(records, target_rate, as_of_date, include_detail=False):
    """Fund every historical entry, injecting only the shortfall when needed.

    Exit proceeds become a separate recycled-cash pool.  This makes every buy
    auditable as recycled cash, fresh external cash, or a mixture of both.
    """
    records_by_buy_date = defaultdict(list)
    all_days = set()
    for record in records:
        records_by_buy_date[record["buy_date"]].append(record)
        all_days.add(record["buy_date"])
        all_days.update(record["bars"].keys())
    days = sorted(day for day in all_days if day <= as_of_date)
    if not days:
        return None

    cash = external_cash = recycled_cash = 0.0
    external_funding = recycled_deployment = 0.0
    open_positions, ledger, curve = [], [], []
    bets_taken = self_funded_bets = target_exits = stop_exits = five_year_exits = 0
    exit_holds, last_external_day = [], None
    peak_deployed = 0.0

    def market_value(exclude=None):
        return sum(
            item["qty"] * item.get("last_close", item["entry"])
            for item in open_positions if item is not exclude
        )

    def locked_value():
        return sum(
            item["qty"] * item.get("last_close", item["entry"])
            for item in open_positions
            if item.get("last_close", item["entry"]) <= item["entry"] * .8
        )

    def add_ledger(event, item, before, external_added=0, recycled_used=0, invested=None, proceeds=None, exclude=None):
        if not include_detail:
            return
        ledger.append({
            "date": day.isoformat(), "event": event, "ticker": item["ticker"],
            "bet_no": item.get("bet_no"), "cash_before": round(before, 2),
            "external_added": round(external_added, 2), "recycled_used": round(recycled_used, 2),
            "invested": round(invested, 2) if invested is not None else None,
            "exit_proceeds": round(proceeds, 2) if proceeds is not None else None,
            "cash_after": round(cash, 2), "cumulative_external": round(external_funding, 2),
            "cumulative_recycled": round(recycled_deployment, 2),
            "portfolio_value": round(cash + market_value(exclude), 2),
        })

    for day in days:
        survivors = []
        for position in open_positions:
            bar = position["bars"].get(day)
            if bar:
                position["last_close"] = bar["close"]
            hold_days = (day - position["buy_date"]).days
            actual_exit = target_rate == "actual" and position.get("actual_sell_date") and day >= position["actual_sell_date"] and position.get("actual_sell_price") is not None
            stop_hit = bool(bar and bar["low"] <= position["entry"] * STOP_LOSS_FACTOR)
            target_hit = bool(isinstance(target_rate, (int, float)) and bar and bar["high"] >= position["entry"] * (1 + target_rate))
            exit_price = exit_reason = None
            if actual_exit:
                exit_price, exit_reason = position["actual_sell_price"], "Actual recorded exit"
            elif stop_hit and target_hit:
                exit_price, exit_reason = position["entry"] * STOP_LOSS_FACTOR, "Ambiguous day — conservative −75% exit"
                stop_exits += 1
            elif stop_hit:
                exit_price, exit_reason = position["entry"] * STOP_LOSS_FACTOR, "−75% exit"
                stop_exits += 1
            elif hold_days >= MAX_HOLD_DAYS:
                exit_price = (bar or {}).get("close") or position.get("last_close") or position["entry"]
                exit_reason = "5-year exit"
                five_year_exits += 1
            elif target_hit:
                exit_price, exit_reason = position["entry"] * (1 + target_rate), "Profit target"
                target_exits += 1
            if exit_price is None:
                survivors.append(position)
                continue
            before = cash
            proceeds = position["qty"] * exit_price
            cash += proceeds
            recycled_cash += proceeds
            exit_holds.append(hold_days)
            add_ledger(exit_reason, position, before, proceeds=proceeds, exclude=position)
        open_positions = survivors

        for record in sorted(records_by_buy_date.get(day, []), key=lambda item: (item["bet_no"] or 0, item["id"] or 0)):
            before = cash
            shortfall = max(0.0, record["capital"] - cash)
            if shortfall:
                cash += shortfall
                external_cash += shortfall
                external_funding += shortfall
                last_external_day = day
            recycled_used = min(recycled_cash, record["capital"])
            external_used = record["capital"] - recycled_used
            recycled_cash -= recycled_used
            external_cash -= external_used
            cash -= record["capital"]
            recycled_deployment += recycled_used
            bets_taken += 1
            if external_used <= .01:
                self_funded_bets += 1
            position = {**record, "last_close": record["entry"]}
            open_positions.append(position)
            add_ledger("Buy", position, before, shortfall, recycled_used, record["capital"])

        deployed = market_value()
        peak_deployed = max(peak_deployed, deployed)
        if include_detail:
            curve.append({
                "date": day.isoformat(), "external": round(external_funding, 2),
                "recycled_cash": round(recycled_cash, 2), "value": round(cash + deployed, 2),
                "locked": round(locked_value(), 2),
            })

    final_value = cash + market_value()
    final_locked = locked_value()
    trailing_days = max(30, len(days) // 4)
    plateau_cutoff = days[-min(trailing_days, len(days))]
    funding_plateau = last_external_day is None or last_external_day < plateau_cutoff
    funding_mix = recycled_deployment / (recycled_deployment + external_funding) * 100 if recycled_deployment + external_funding else 0
    self_funded_pct = self_funded_bets / bets_taken * 100 if bets_taken else 0
    locked_pct = final_locked / final_value * 100 if final_value else 0
    score = min(100, max(0, self_funded_pct * .55 + funding_mix * .25 + (10 if funding_plateau else 0) + max(0, 10 - locked_pct * .25)))
    return {
        "label": "Actual" if target_rate == "actual" else "No target" if target_rate is None else f"{target_rate * 100:.0f}%",
        "target_rate": target_rate, "external_funding": round(external_funding, 2),
        "recycled_deployment": round(recycled_deployment, 2), "self_funded_pct": round(self_funded_pct, 1),
        "peak_deployed": round(peak_deployed, 2), "capital_locked": round(final_locked, 2),
        "bets_taken": bets_taken, "target_exits": target_exits, "stop_exits": stop_exits,
        "five_year_exits": five_year_exits, "avg_hold_days": round(sum(exit_holds) / len(exit_holds), 1) if exit_holds else None,
        "final_value": round(final_value, 2), "funding_plateau": funding_plateau,
        "funding_mix_pct": round(funding_mix, 1), "score": round(score, 1),
        "ledger": ledger if include_detail else None, "curve": curve if include_detail else None,
    }


def simulate_funding_sustainability(positions, as_of_date=None, history_provider=daily_ohlc, detail_target=0.07):
    as_of_date = as_of_date or date.today()
    records, missing_history = _position_records(positions, history_provider, as_of_date)
    targets = (None, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.26)
    scenarios = [_run_funding_scenario(records, target, as_of_date) for target in targets]
    scenarios = [item for item in scenarios if item]
    detail = _run_funding_scenario(records, detail_target, as_of_date, include_detail=True)
    recommended = next((item for item in scenarios if item["target_rate"] == 0.07), None)
    return {
        "as_of_date": as_of_date.isoformat(), "records_analysed": len(records),
        "missing_history": sorted(set(missing_history)), "scenarios": scenarios, "detail": detail,
        "recommended": recommended,
        "rules_note": "Every historical stock bet is funded in date order. When cash is short, only the shortfall is injected as external funding; all exit proceeds then become recycled cash available for the next bet. Profit targets use daily High; −75% exits use daily Low; five-year exits use daily Close.",
    }
