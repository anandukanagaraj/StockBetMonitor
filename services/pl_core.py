"""Stock and option P&L calculations shared by the web app, API and scheduler."""
import calendar
from collections import defaultdict
from datetime import date
from services.date_utils import normalized_buy_date_map
from services.date_utils import parse_date as parse_shared_date
from services.date_utils import position_identity
from services.number_format import format_indian_money
from services.option_charges import option_strategy_risk_capital


def money(value):
    return format_indian_money(value)


def option_strategy_return_pct(row):
    if row.get("strategy_pl") is None:
        return None
    capital = option_strategy_risk_capital(row)
    if not capital:
        return None
    return (row["strategy_pl"] or 0) / capital * 100


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


def api_float(value):
    return round(float(value), 2) if value is not None else None


def api_pct(value):
    return round(float(value), 4) if value is not None else None


def row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


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
