from services.option_charges import (
    calculated_option_max_risk,
    cost_breakdown,
    net_after_costs,
    option_leg_type,
    option_strategy_costs,
    option_strategy_risk_capital,
    stock_simulation_costs,
    stock_simulation_pl_from_move,
)


def test_option_leg_type_from_ticker_suffix():
    assert option_leg_type({"ticker": "NIFTY 25000 CE"}) == "CE"
    assert option_leg_type({"ticker": "NIFTY 25000 PE"}) == "PE"


def test_long_call_max_risk_uses_debit_paid():
    row = {
        "strategy_type": "Long Call",
        "legs": [
            {"action": "BUY", "qty": 50, "buy_price": 120, "option_type": "CE", "strike": 25000},
        ],
    }
    assert calculated_option_max_risk(row) == 6000


def test_option_strategy_costs_include_brokerage_and_gst():
    legs = [
        {
            "action": "BUY",
            "qty": 50,
            "buy_price": 120,
            "sell_price": 150,
            "option_type": "CE",
            "strike": 25000,
        }
    ]
    costs = option_strategy_costs(legs)
    assert costs["brokerage"] == 40
    assert costs["stt"] > 0
    assert costs["total"] > costs["brokerage"]


def test_net_after_costs_subtracts_total_charges():
    costs = cost_breakdown(brokerage=40, stt=100, transaction=10, sebi=1, stamp=2)
    assert net_after_costs(5000, costs) == 5000 - costs["total"]


def test_stock_simulation_pl_for_long_side():
    assert stock_simulation_pl_from_move(10, "Long Call", 100000) == 10000


def test_stock_simulation_pl_for_short_side():
    assert stock_simulation_pl_from_move(10, "Long Put", 100000) == -10000


def test_stock_simulation_costs_include_dp_for_long_equivalent():
    costs = stock_simulation_costs(10, "Long Call", 100000)
    assert costs is not None
    assert costs["dp"] > 0
    assert costs["total"] > 0


def test_option_strategy_risk_capital_prefers_margin():
    row = {
        "margin": 25000,
        "max_risk": 10000,
        "strategy_type": "Iron Condor",
        "legs": [],
    }
    assert option_strategy_risk_capital(row) == 25000
