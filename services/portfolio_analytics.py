from collections import defaultdict
from datetime import date, datetime

from db import is_closed
from services.date_utils import parse_date


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _stock_realized_pl(row):
    if row["sell_price"] is None:
        return None
    return (row["sell_price"] - row["buy_price"]) * row["qty"]


def _stock_unrealized_pl(row):
    if is_closed(row):
        return None
    current_price = row["current_price"]
    if current_price is None:
        return None
    return (current_price - row["buy_price"]) * row["qty"]


def _stock_open_capital(row):
    if is_closed(row):
        return 0
    return (row["buy_price"] or 0) * (row["qty"] or 0)


def _is_open_option(row):
    return (_row_get(row, "status") or "").lower() != "closed"


def _closed_transactions(positions, strategies):
    transactions = []

    for row in positions:
        if not is_closed(row) or row["sell_price"] is None:
            continue
        sold_at = parse_date(row["sell_date"])
        if not sold_at:
            continue
        profit = _stock_realized_pl(row) or 0
        transactions.append(
            {
                "date": sold_at,
                "source": "Stock",
                "label": f"Bet {row['bet_no'] or '-'} - {row['ticker']}",
                "pl": profit,
                "outcome": "win" if profit > 0 else "loss" if profit < 0 else "flat",
            }
        )

    for row in strategies:
        if not _is_open_option(row) and _row_get(row, "strategy_pl") is not None:
            sold_at = parse_date(row["sell_date"])
            if not sold_at:
                continue
            profit = row["strategy_pl"] or 0
            transactions.append(
                {
                    "date": sold_at,
                    "source": "Option",
                    "label": f"{_row_get(row, 'strategy_type') or 'Option'} #{_row_get(row, 'bet_no') or _row_get(row, 'strategy_no')}",
                    "pl": profit,
                    "outcome": "win" if profit > 0 else "loss" if profit < 0 else "flat",
                }
            )

    return sorted(transactions, key=lambda item: (item["date"], item["source"], item["label"]))


def _equity_curve_points(transactions, unrealized_pl_total, performance_snapshots=None):
    if not transactions and not unrealized_pl_total and not performance_snapshots:
        return []

    daily = defaultdict(float)
    for item in transactions:
        daily[item["date"]] += item["pl"]

    points = []
    cumulative = 0.0
    for day in sorted(daily.keys()):
        day_pl = daily[day]
        cumulative += day_pl
        points.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%d %b %Y"),
                "source": "Daily",
                "pl": round(day_pl, 2),
                "value": round(cumulative, 2),
                "outcome": "win" if day_pl > 0 else "loss" if day_pl < 0 else "flat",
            }
        )

    snapshots = performance_snapshots or []
    if snapshots:
        first_snapshot_day = str(snapshots[0]["snapshot_at"])[:10]
        points = [point for point in points if point["date"] < first_snapshot_day]
        for snapshot in snapshots:
            snapshot_at = str(snapshot["snapshot_at"])
            captured_at = datetime.fromisoformat(snapshot_at)
            total_pl = round(snapshot["total_pl"], 2)
            previous_value = points[-1]["value"] if points else 0.0
            snapshot_pl = round(total_pl - previous_value, 2)
            points.append(
                {
                    "date": snapshot_at,
                    "label": captured_at.strftime("%d %b %H:%M"),
                    "source": "Price snapshot",
                    "pl": snapshot_pl,
                    "value": total_pl,
                    "outcome": "win" if snapshot_pl > 0 else "loss" if snapshot_pl < 0 else "flat",
                }
            )

    now = datetime.now().replace(microsecond=0)
    current_total = round(cumulative + unrealized_pl_total, 2)
    if not points or abs(points[-1]["value"] - current_total) > 0.009:
        previous_value = points[-1]["value"] if points else 0.0
        points.append(
            {
                "date": now.isoformat(),
                "label": "Now" if snapshots else "Today",
                "source": "Mark-to-market",
                "pl": round(current_total - previous_value, 2),
                "value": current_total,
                "outcome": "flat",
            }
        )
    return points


def _drawdown_metrics(equity_points):
    if not equity_points:
        return {
            "current_pct": None,
            "current_amount": None,
            "max_pct": None,
            "max_amount": None,
            "peak_value": None,
            "peak_date": None,
            "series": [],
        }

    series = []
    peak_value = equity_points[0]["value"]
    peak_date = equity_points[0]["date"]
    max_drawdown_amount = 0.0
    max_drawdown_pct = 0.0
    current_drawdown_amount = 0.0
    current_drawdown_pct = 0.0

    for point in equity_points:
        value = point["value"]
        if value > peak_value:
            peak_value = value
            peak_date = point["date"]
        drawdown_amount = value - peak_value
        drawdown_pct = (drawdown_amount / peak_value * 100) if peak_value else 0
        if drawdown_amount < max_drawdown_amount:
            max_drawdown_amount = drawdown_amount
            max_drawdown_pct = drawdown_pct
        if point["date"] == equity_points[-1]["date"]:
            current_drawdown_amount = drawdown_amount
            current_drawdown_pct = drawdown_pct
        series.append(
            {
                "date": point["date"],
                "label": point["label"],
                "value": round(value, 2),
                "peak": round(peak_value, 2),
                "drawdown_amount": round(drawdown_amount, 2),
                "drawdown_pct": round(drawdown_pct, 2),
            }
        )

    return {
        "current_pct": round(current_drawdown_pct, 2),
        "current_amount": round(current_drawdown_amount, 2),
        "max_pct": round(max_drawdown_pct, 2),
        "max_amount": round(max_drawdown_amount, 2),
        "peak_value": round(peak_value, 2),
        "peak_date": peak_date,
        "series": series,
    }


def _allocation_rows(items, total):
    rows = []
    for item in sorted(items, key=lambda row: row["capital"], reverse=True):
        pct = (item["capital"] / total * 100) if total else 0
        rows.append({**item, "pct": round(pct, 1)})
    return rows


def _stock_bet_activity(positions):
    events = []
    for row in positions:
        buy_date = parse_date(_row_get(row, "buy_date"))
        qty, buy_price = _row_get(row, "qty") or 0, _row_get(row, "buy_price") or 0
        common = {"bet_no": _row_get(row, "bet_no"), "ticker": _row_get(row, "ticker") or "-",
                  "group": _row_get(row, "trade_group_name") or _row_get(row, "trade_group_code") or "Ungrouped",
                  "qty": qty, "buy_price": buy_price}
        if buy_date:
            events.append({**common, "date": buy_date, "event": "Opened", "amount": buy_price * qty,
                           "sell_price": None, "realized_pl": None})
        sell_date = parse_date(_row_get(row, "sell_date"))
        if is_closed(row) and sell_date:
            sell_price = _row_get(row, "sell_price")
            events.append({**common, "date": sell_date, "event": "Closed",
                           "amount": (sell_price or 0) * qty if sell_price is not None else 0,
                           "sell_price": sell_price,
                           "realized_pl": ((sell_price - buy_price) * qty) if sell_price is not None else None})

    for item in events:
        day = item["date"]
        iso_year, iso_week, _ = day.isocalendar()
        item.update({"date_iso": day.isoformat(), "date_label": day.strftime("%d %b %Y"),
                     "week": f"{iso_year}-W{iso_week:02d}", "week_label": f"Week {iso_week}, {iso_year}",
                     "month": day.strftime("%Y-%m"), "month_label": day.strftime("%b %Y"),
                     "quarter": f"{day.year}-Q{(day.month - 1) // 3 + 1}",
                     "quarter_label": f"Q{(day.month - 1) // 3 + 1} {day.year}",
                     "year": str(day.year), "year_label": str(day.year)})

    rollups = {}
    for grain in ("week", "month", "quarter", "year"):
        buckets = {}
        for item in events:
            bucket = buckets.setdefault(item[grain], {"key": item[grain], "label": item[f"{grain}_label"],
                "opened": 0, "closed": 0, "opened_capital": 0.0, "closed_proceeds": 0.0, "realized_pl": 0.0})
            if item["event"] == "Opened":
                bucket["opened"] += 1; bucket["opened_capital"] += item["amount"]
            else:
                bucket["closed"] += 1; bucket["closed_proceeds"] += item["amount"]
                bucket["realized_pl"] += item["realized_pl"] or 0
        rollups[grain] = sorted(buckets.values(), key=lambda x: x["key"], reverse=True)
    return {"events": sorted(events, key=lambda x: (x["date"], x["event"], x["bet_no"] or 0), reverse=True), "rollups": rollups}


def _stock_capital_forecast(positions, forecast_days=365, reserve_pct=15):
    """Estimate stock-only capacity from the portfolio's observed trade flow.

    This intentionally models net capital build-up (new buys less closed bets),
    rather than assuming every new bet is immediately funded by a closed one.
    That makes the estimate suitable for the current portfolio, where open bets
    materially exceed closures.
    """
    dated_rows = []
    closed_count = 0
    current_open_capital = 0.0

    for row in positions:
        buy_date = parse_date(_row_get(row, "buy_date"))
        capital = _stock_open_capital(row) if not is_closed(row) else (
            (_row_get(row, "buy_price") or 0) * (_row_get(row, "qty") or 0)
        )
        if buy_date and capital > 0:
            dated_rows.append((buy_date, capital))
        if is_closed(row):
            closed_count += 1
        else:
            current_open_capital += _stock_open_capital(row)

    if not dated_rows:
        return None

    first_buy_date = min(item[0] for item in dated_rows)
    observed_days = max(1, (date.today() - first_buy_date).days + 1)
    total_bets = len(dated_rows)
    average_bet_capital = sum(item[1] for item in dated_rows) / total_bets
    entries_per_day = total_bets / observed_days
    closures_per_day = closed_count / observed_days
    projected_entries = entries_per_day * forecast_days
    projected_closures = min(projected_entries, closures_per_day * forecast_days)
    projected_net_bets = max(0.0, projected_entries - projected_closures)
    projected_net_capital = projected_net_bets * average_bet_capital
    working_capital = current_open_capital + projected_net_capital
    reserve_capital = working_capital * reserve_pct / 100
    closure_gap_per_day = max(0.0, entries_per_day - closures_per_day)
    additional_closures_annual = closure_gap_per_day * forecast_days
    current_cycle_reserve = current_open_capital * reserve_pct / 100

    return {
        "forecast_days": forecast_days,
        "reserve_pct": reserve_pct,
        "as_of_date": date.today().isoformat(),
        "first_buy_date": first_buy_date.isoformat(),
        "observed_days": observed_days,
        "bets_observed": total_bets,
        "closed_bets_observed": closed_count,
        "open_bets_observed": total_bets - closed_count,
        "average_bet_capital": round(average_bet_capital, 2),
        "entries_per_day": round(entries_per_day, 3),
        "closures_per_day": round(closures_per_day, 3),
        "break_even_closures_per_day": round(entries_per_day, 3),
        "closure_gap_per_day": round(closure_gap_per_day, 3),
        "additional_closures_annual": round(additional_closures_annual),
        "projected_entries": round(projected_entries),
        "projected_closures": round(projected_closures),
        "projected_net_bets": round(projected_net_bets),
        "current_open_capital": round(current_open_capital, 2),
        "projected_net_capital": round(projected_net_capital, 2),
        "working_capital": round(working_capital, 2),
        "reserve_capital": round(reserve_capital, 2),
        "current_cycle_reserve": round(current_cycle_reserve, 2),
        "recommended_capital": round(working_capital + reserve_capital, 2),
        "additional_capital": round(max(0.0, working_capital + reserve_capital - current_open_capital), 2),
    }


def _investment_funding_breakdown(positions):
    """Replay capital flows, assuming every available sale proceed is retained.

    The application does not store deposits or withdrawals.  This therefore
    reports the minimum outside cash required to fund recorded entries, with
    same-day closures available before same-day buys.  Recycled principal is
    consumed before realized profit so the attribution remains deterministic.
    """
    events = []

    for row in positions:
        capital = (_row_get(row, "buy_price") or 0) * (_row_get(row, "qty") or 0)
        bought_at = parse_date(_row_get(row, "buy_date"))
        label = f"Stock bet {_row_get(row, 'bet_no') or '-'} - {_row_get(row, 'ticker') or '-'}"
        if bought_at and capital > 0:
            events.append({"date": bought_at, "order": 1, "kind": "entry", "label": label, "capital": capital})
        sold_at = parse_date(_row_get(row, "sell_date"))
        if is_closed(row) and sold_at and _row_get(row, "sell_price") is not None:
            events.append({"date": sold_at, "order": 0, "kind": "exit", "label": label,
                           "capital": capital, "pl": _stock_realized_pl(row) or 0})

    principal_cash = 0.0
    profit_cash = 0.0
    deposited = 0.0
    total_entries = 0.0
    principal_reinvested = 0.0
    profit_reinvested = 0.0
    principal_returned = 0.0
    positive_profit = 0.0
    rows = []

    for event in sorted(events, key=lambda item: (item["date"], item["order"], item["label"])):
        if event["kind"] == "exit":
            returned = max(0.0, event["capital"] + min(0.0, event["pl"]))
            earned = max(0.0, event["pl"])
            principal_cash += returned
            profit_cash += earned
            principal_returned += returned
            positive_profit += earned
            continue

        needed = event["capital"]
        from_principal = min(needed, principal_cash)
        principal_cash -= from_principal
        needed -= from_principal
        from_profit = min(needed, profit_cash)
        profit_cash -= from_profit
        needed -= from_profit
        new_deposit = needed
        deposited += new_deposit
        total_entries += event["capital"]
        principal_reinvested += from_principal
        profit_reinvested += from_profit
        rows.append({
            "date": event["date"].isoformat(),
            "label": event["label"],
            "capital": round(event["capital"], 2),
            "closed_capital_reinvested": round(from_principal, 2),
            "profit_reinvested": round(from_profit, 2),
            "new_deposit": round(new_deposit, 2),
        })

    return {
        "estimated_deposited": round(deposited, 2),
        "total_entry_capital": round(total_entries, 2),
        "closed_capital_returned": round(principal_returned, 2),
        "closed_capital_reinvested": round(principal_reinvested, 2),
        "realized_profit_available": round(positive_profit, 2),
        "profit_reinvested": round(profit_reinvested, 2),
        "cash_remaining": round(principal_cash + profit_cash, 2),
        "closed_capital_cash_remaining": round(principal_cash, 2),
        "profit_cash_remaining": round(profit_cash, 2),
        "entries": rows,
    }


def build_portfolio_analytics(positions, strategies, option_risk_capital_fn, performance_snapshots=None):
    stock_realized = 0.0
    stock_unrealized = 0.0
    stock_open_capital = 0.0
    open_stock_bets = 0

    stock_groups = defaultdict(lambda: {"open_capital": 0.0, "open_bets": 0, "realized_pl": 0.0})
    for row in positions:
        group_code = _row_get(row, "trade_group_code") or "UNGROUPED"
        group_name = _row_get(row, "trade_group_name") or group_code
        bucket = stock_groups[(group_code, group_name)]
        if is_closed(row) and row["sell_price"] is not None:
            realized = _stock_realized_pl(row) or 0
            stock_realized += realized
            bucket["realized_pl"] += realized
        else:
            capital = _stock_open_capital(row)
            stock_open_capital += capital
            open_stock_bets += 1
            bucket["open_capital"] += capital
            bucket["open_bets"] += 1

    option_realized = 0.0
    option_open_risk = 0.0
    open_option_bets = 0
    option_groups = defaultdict(lambda: {"open_capital": 0.0, "open_bets": 0, "realized_pl": 0.0})

    for row in strategies:
        group_code = _row_get(row, "trade_group_code") or "UNGROUPED"
        group_name = _row_get(row, "trade_group_name") or group_code
        bucket = option_groups[(group_code, group_name)]
        if _is_open_option(row):
            risk = option_risk_capital_fn(row) or 0
            option_open_risk += risk
            open_option_bets += 1
            bucket["open_capital"] += risk
            bucket["open_bets"] += 1
        elif _row_get(row, "strategy_pl") is not None:
            realized = row["strategy_pl"] or 0
            option_realized += realized
            bucket["realized_pl"] += realized

    transactions = _closed_transactions(positions, strategies)
    unrealized_total = stock_unrealized = sum(
        value for row in positions if (value := _stock_unrealized_pl(row)) is not None
    )
    equity_curve = _equity_curve_points(transactions, unrealized_total, performance_snapshots)
    drawdown = _drawdown_metrics(equity_curve)

    open_exposure_total = stock_open_capital + option_open_risk
    stock_group_rows = _allocation_rows(
        [
            {
                "code": code,
                "name": name,
                "capital": values["open_capital"],
                "open_bets": values["open_bets"],
                "realized_pl": round(values["realized_pl"], 2),
            }
            for (code, name), values in stock_groups.items()
            if values["open_capital"] > 0 or values["realized_pl"] != 0
        ],
        stock_open_capital,
    )
    option_group_rows = _allocation_rows(
        [
            {
                "code": code,
                "name": name,
                "capital": values["open_capital"],
                "open_bets": values["open_bets"],
                "realized_pl": round(values["realized_pl"], 2),
            }
            for (code, name), values in option_groups.items()
            if values["open_capital"] > 0 or values["realized_pl"] != 0
        ],
        option_open_risk,
    )
    segment_rows = _allocation_rows(
        [
            {
                "segment": "Stocks",
                "capital": stock_open_capital,
                "open_bets": open_stock_bets,
            },
            {
                "segment": "Options",
                "capital": option_open_risk,
                "open_bets": open_option_bets,
            },
        ],
        open_exposure_total,
    )

    total_realized = stock_realized + option_realized
    total_actual = total_realized + stock_unrealized
    stock_capital_forecast = _stock_capital_forecast(positions)
    investment_funding = _investment_funding_breakdown(positions)
    # Deposited-capital return is based on booked stock profit only. Unrealized
    # mark-to-market movement remains part of Total Portfolio P/L above.
    stock_actual_pl = stock_realized
    estimated_deposited = investment_funding["estimated_deposited"]
    investment_funding["stock_realized_pl"] = round(stock_actual_pl, 2)
    investment_funding["pl_on_deposited_pct"] = (
        round(stock_actual_pl / estimated_deposited * 100, 2)
        if estimated_deposited
        else None
    )

    return {
        "summary": {
            "total_realized_pl": round(total_realized, 2),
            "total_unrealized_pl": round(stock_unrealized, 2),
            "total_actual_pl": round(total_actual, 2),
            "stock_realized_pl": round(stock_realized, 2),
            "option_realized_pl": round(option_realized, 2),
            "stock_unrealized_pl": round(stock_unrealized, 2),
            "stock_total_pl": round(stock_realized + stock_unrealized, 2),
            "open_stock_capital": round(stock_open_capital, 2),
            "open_option_risk_capital": round(option_open_risk, 2),
            "open_exposure_total": round(open_exposure_total, 2),
            "open_stock_bets": open_stock_bets,
            "open_option_bets": open_option_bets,
            "closed_trades": len(transactions),
        },
        "equity_curve": equity_curve,
        "drawdown": drawdown,
        "stock_group_allocation": stock_group_rows,
        "option_group_allocation": option_group_rows,
        "segment_allocation": segment_rows,
        "stock_capital_forecast": stock_capital_forecast,
        "investment_funding": investment_funding,
        "stock_bet_activity": _stock_bet_activity(positions),
    }
