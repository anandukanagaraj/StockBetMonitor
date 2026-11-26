"""Options dashboard metrics and what-if report calculations."""
import math
from services.option_charges import add_costs
from services.option_charges import empty_costs
from services.option_charges import net_after_costs
from services.option_charges import option_leg_type
from services.option_charges import option_strategy_costs
from services.option_charges import option_strategy_risk_capital
from services.option_charges import single_leg_amount_risked
from services.option_charges import single_leg_simulation_costs
from services.option_charges import stock_equivalent_notional
from services.option_charges import stock_simulation_costs
from services.option_charges import stock_simulation_label
from services.option_charges import stock_simulation_pl_from_move
from services.option_charges import stock_simulation_side
from services.app_config import BUSINESS_INCOME_TAX_RATE, STCG_TAX_RATE, WHATIF_TRADE_LIMIT
from services.pl_core import money


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
