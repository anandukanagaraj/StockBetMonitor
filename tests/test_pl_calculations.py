from services.portfolio_analytics import build_portfolio_analytics, _stock_bet_activity


def test_stock_bet_activity_rolls_up_opened_and_closed_periods():
    rows = [_position(bet_no=1, ticker='AAA', buy_date='2026-01-30', sell_date='2026-02-03', sell_price=120, status='Closed'),
            _position(id=2, bet_no=2, ticker='BBB', buy_date='2026-02-04', qty=5, buy_price=200)]
    activity = _stock_bet_activity(rows)
    feb = next(x for x in activity['rollups']['month'] if x['key']=='2026-02')
    assert feb['opened'] == 1 and feb['closed'] == 1
    assert feb['opened_capital'] == 1000 and feb['closed_proceeds'] == 1200
    assert feb['realized_pl'] == 200
    assert len(activity['events']) == 3
    assert {'week','month','quarter','year'} <= activity['events'][0].keys()
from services.option_charges import option_strategy_risk_capital


def _position(**overrides):
    base = {
        "id": 1,
        "bet_no": 1,
        "trade_group_code": "SWING",
        "trade_group_name": "Swing Trading",
        "ticker": "TEST",
        "buy_date": "2026-01-01",
        "qty": 10,
        "buy_price": 100,
        "target_price": 120,
        "stop_loss": None,
        "current_price": 110,
        "sell_date": None,
        "sell_price": None,
        "status": "Open",
    }
    base.update(overrides)
    return base


def test_stock_realized_pl_in_equity_curve():
    positions = [
        _position(
            sell_date="2026-02-01",
            sell_price=120,
            status="Closed",
        )
    ]
    analytics = build_portfolio_analytics(positions, [], option_strategy_risk_capital)
    assert analytics["summary"]["stock_realized_pl"] == 200
    assert analytics["summary"]["stock_total_pl"] == 200
    assert analytics["equity_curve"][-1]["value"] == 200


def test_stock_unrealized_pl_included_in_today_point():
    positions = [_position(current_price=115)]
    analytics = build_portfolio_analytics(positions, [], option_strategy_risk_capital)
    assert analytics["summary"]["stock_unrealized_pl"] == 150
    assert analytics["equity_curve"][-1]["label"] == "Today"


def test_segment_allocation_splits_stock_and_option_exposure():
    positions = [_position()]
    strategies = [
        {
            "status": "Open",
            "strategy_type": "Long Call",
            "margin": 5000,
            "trade_group_code": "BTST",
            "trade_group_name": "BTST",
            "legs": [],
        }
    ]
    analytics = build_portfolio_analytics(positions, strategies, option_strategy_risk_capital)
    segments = {row["segment"]: row for row in analytics["segment_allocation"]}
    assert segments["Stocks"]["capital"] == 1000
    assert segments["Options"]["capital"] == 5000


def test_investment_funding_tracks_deposit_and_reinvestment():
    positions = [
        _position(id=1, bet_no=1, buy_date="2026-01-01", qty=10, buy_price=100,
                  sell_date="2026-01-10", sell_price=120, status="Closed"),
        _position(id=2, bet_no=2, buy_date="2026-01-11", qty=11, buy_price=100),
        _position(id=3, bet_no=3, buy_date="2026-01-12", qty=2, buy_price=100),
    ]
    funding = build_portfolio_analytics(positions, [], option_strategy_risk_capital)["investment_funding"]
    assert funding["estimated_deposited"] == 1100
    assert funding["closed_capital_reinvested"] == 1000
    assert funding["profit_reinvested"] == 200
    assert funding["cash_remaining"] == 0
    assert funding["stock_realized_pl"] == 200
    assert funding["pl_on_deposited_pct"] == 18.18


def test_investment_funding_excludes_options():
    strategies = [{
        "status": "Open", "strategy_type": "Long Call", "buy_date": "2026-01-01",
        "margin": 5000, "trade_group_code": "BTST", "trade_group_name": "BTST", "legs": [],
    }]
    funding = build_portfolio_analytics([], strategies, option_strategy_risk_capital)["investment_funding"]
    assert funding["estimated_deposited"] == 0
    assert funding["total_entry_capital"] == 0
    assert funding["entries"] == []
    assert funding["pl_on_deposited_pct"] is None


def test_stock_capital_forecast_reports_fresh_capital_break_even():
    analytics = build_portfolio_analytics(
        [_position(buy_date="2026-01-01"), _position(id=2, bet_no=2, buy_date="2026-01-02")],
        [], option_strategy_risk_capital,
    )
    plan = analytics["stock_capital_forecast"]
    assert plan["break_even_closures_per_day"] == plan["entries_per_day"]
    assert plan["additional_closures_annual"] >= 0
    assert plan["current_cycle_reserve"] == plan["current_open_capital"] * 0.15
