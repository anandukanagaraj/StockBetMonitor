import re

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


def option_leg_strike(leg):
    if leg.get("strike") is not None:
        return leg["strike"]
    ticker = (leg.get("ticker") or "").strip().upper()
    match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)\s+(?:CE|PE)$", ticker)
    return float(match.group(1)) if match else None


def option_strategy_capital(row):
    capital = row.get("total_trade_capital")
    if not capital:
        capital = sum(
            (leg.get("trade_capital") or ((leg.get("qty") or 0) * (leg.get("buy_price") or 0)))
            for leg in row.get("legs", [])
        )
    return capital or None


def option_leg_net_cashflow(leg):
    qty = leg.get("qty") or 0
    buy_price = leg.get("buy_price") or 0
    value = buy_price * qty
    return -value if (leg.get("action") or "BUY").upper() == "SELL" else value


def option_spread_width(legs, option_type_filter=None):
    filtered = [
        leg
        for leg in legs
        if option_leg_strike(leg) is not None
        and (option_type_filter is None or option_leg_type(leg) == option_type_filter)
    ]
    if len(filtered) < 2:
        return None
    qty = max(abs(leg.get("qty") or 0) for leg in filtered)
    strikes = [option_leg_strike(leg) for leg in filtered]
    return (max(strikes) - min(strikes)) * qty


def calculated_option_max_risk(row):
    legs = row.get("legs") or []
    if not legs:
        return None
    strategy = (row.get("strategy_type") or "").strip().lower()
    debit = sum(option_leg_net_cashflow(leg) for leg in legs)
    if strategy in {"long call", "long put", "bull call spread", "bear put spread", "debit spread"}:
        return max(debit, 0) or None
    if strategy in {"bull put spread", "bear call spread", "credit spread"}:
        width = option_spread_width(legs)
        return max((width or 0) - abs(min(debit, 0)), 0) or None
    if strategy == "iron condor":
        width = max(option_spread_width(legs, "CE") or 0, option_spread_width(legs, "PE") or 0)
        return max(width - abs(min(debit, 0)), 0) or None
    if strategy == "iron butterfly":
        width = option_spread_width(legs)
        return max((width or 0) - abs(min(debit, 0)), 0) or None
    return max(debit, 0) or None


def option_strategy_risk_capital(row):
    if row.get("margin"):
        return row["margin"]
    if row.get("max_risk"):
        return row["max_risk"]
    calculated_risk = calculated_option_max_risk(row)
    if calculated_risk:
        return calculated_risk
    return option_strategy_capital(row)


def stock_simulation_side(strategy_type):
    strategy = (strategy_type or "").strip().lower()
    if strategy in {"bear put spread", "long put", "bear call spread"}:
        return "short"
    return "long"


def stock_simulation_label(strategy_type):
    return "Short Stock Equivalent" if stock_simulation_side(strategy_type) == "short" else "Long Stock Equivalent"


def stock_equivalent_notional(strategy_type, legs):
    side = stock_simulation_side(strategy_type)
    preferred_type = "PE" if side == "short" else "CE"
    buy_legs = [
        leg
        for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and option_leg_type(leg) == preferred_type
        and leg.get("qty") is not None
        and option_leg_strike(leg) is not None
    ]
    fallback_legs = [
        leg
        for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and leg.get("qty") is not None
        and option_leg_strike(leg) is not None
    ]
    all_legs = [
        leg
        for leg in legs
        if leg.get("qty") is not None
        and option_leg_strike(leg) is not None
    ]
    candidates = buy_legs or fallback_legs or all_legs
    if not candidates:
        return None
    leg = max(candidates, key=lambda item: abs(item["qty"] or 0))
    return abs(leg["qty"] or 0) * (option_leg_strike(leg) or 0)


def stock_simulation_pl_from_move(stock_move_pct, strategy_type, notional):
    if stock_move_pct is None or notional is None:
        return None
    direction = -1 if stock_simulation_side(strategy_type) == "short" else 1
    return direction * notional * stock_move_pct / 100


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
    return sum(
        rounded_charge((leg.get("sell_price") or 0) * (leg.get("qty") or 0) * OPTION_STT_RATE)
        for leg in eligible_legs
    )


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


def single_leg_amount_risked(legs, option_type):
    return sum(
        (leg.get("trade_capital") or ((leg.get("qty") or 0) * (leg.get("buy_price") or 0)))
        for leg in legs
        if (leg.get("action") or "").upper() == "BUY"
        and option_leg_type(leg) == option_type
        and leg.get("achieved_pl") is not None
    )


def stock_simulation_brokerage(stock_move_pct):
    return 0 if stock_move_pct is not None else None


def stock_simulation_stt(stock_move_pct, strategy_type, notional):
    if stock_move_pct is None or notional is None:
        return None
    side = stock_simulation_side(strategy_type)
    if side == "short":
        sell_value = notional
        buy_stt = 0
    else:
        buy_stt = notional * EQUITY_DELIVERY_STT_RATE
        sell_value = notional * (1 + stock_move_pct / 100)
    sell_stt = max(0, sell_value) * EQUITY_DELIVERY_STT_RATE
    return rounded_charge(buy_stt + sell_stt)


def stock_simulation_costs(stock_move_pct, strategy_type, notional):
    if stock_move_pct is None or notional is None:
        return None
    side = stock_simulation_side(strategy_type)
    if side == "short":
        sell_value = notional
        buy_value = max(0, notional * (1 + stock_move_pct / 100))
        dp = 0
    else:
        buy_value = notional
        sell_value = max(0, notional * (1 + stock_move_pct / 100))
        dp = EQUITY_DP_CHARGE
    turnover = buy_value + sell_value
    return cost_breakdown(
        brokerage=0,
        stt=stock_simulation_stt(stock_move_pct, strategy_type, notional) or 0,
        transaction=turnover * EQUITY_DELIVERY_TRANSACTION_RATE,
        sebi=turnover * SEBI_RATE,
        stamp=buy_value * EQUITY_DELIVERY_STAMP_RATE,
        dp=dp,
    )


def net_after_costs(pl_value, costs):
    if pl_value is None:
        return None
    if isinstance(costs, dict):
        return pl_value - (costs.get("total") or 0)
    return pl_value - (costs or 0)
