from datetime import date

from services.stock_profit_booking_simulator import stop_loss_result, history_highs, cache_key
from services.stock_profit_booking_simulator import stop_comparison
from services.stock_profit_booking_simulator import include_purchase_day
import pytest


@pytest.mark.parametrize('price,status', [(80,'At/below stop'),(125,'At/above target'),(110,'Between stop and target')])
def test_today_keeps_mtm_without_fabricating_exit(price, status):
    today = date(2026, 9, 4)
    p = dict(current_price=price, target_price=120, buy_price=100, qty=10, status='Open', sell_price=None, sell_date=None)
    r = include_purchase_day(stop_loss_result([], today, 100, 10, (price-100)*10), p, today, today)
    assert r['available'] and r['purchase_day']
    assert not r['hit'] and r['exit_price'] is None
    assert r['pl'] == (price-100)*10
    assert r['delta'] == 0
    assert r['current_status'] == status


def test_missing_price_or_older_history_not_made_comparable():
    today = date(2026, 9, 4)
    p = dict(current_price=None, target_price=120, buy_price=100, qty=10, status='Open', sell_price=None, sell_date=None)
    r = stop_loss_result([], today, 100, 10, None)
    assert not include_purchase_day(r, p, today, today)['available']
    p['current_price'] = 110
    assert not include_purchase_day(r, p, date(2026, 9, 3), today)['available']


def test_comparison_moves_stopped_open_bet_to_realized():
    positions = [dict(status="Closed", sell_price=120, sell_date="2026-08-04", buy_price=100, qty=10, current_price=120),
                 dict(status="Open", sell_price=None, sell_date=None, buy_price=100, qty=10, current_price=80),
                 dict(status="Open", sell_price=None, sell_date=None, buy_price=100, qty=10, current_price=110)]
    rows = [dict(actual_closed=True, actual_pl=200, capital=1000, stop_loss=dict(delta=0, pl=200, hit=False)),
            dict(actual_closed=False, actual_pl=-200, capital=1000, qty=10, stop_loss=dict(delta=50, pl=-150, hit=True, exit_price=85)),
            dict(actual_closed=False, actual_pl=100, capital=1000, stop_loss=dict(delta=None, pl=None, hit=False))]
    result = stop_comparison(rows, positions)
    assert result['all_actual']['realized_pl'] == 200
    assert result['all_actual']['unrealized_pl'] == -100
    assert result['actual']['unrealized_pl'] == -200
    assert result['simulated']['realized_pl'] == 50
    assert result['simulated']['unrealized_pl'] == 0
    assert result['simulated']['closed_count'] == 2
    assert result['simulated']['open_count'] == 0
    assert result['excluded'] == 1
    assert result['differences']['total_pl'] == 50
    assert result['stop_realized'] == -150
    assert result['other_realized'] == 200
    assert result['released_cash'] == 850
    assert result['stop_realized'] + result['other_realized'] == result['simulated']['realized_pl']


def test_75_percent_stop_and_gap():
    bars = [{'date': '2026-08-04', 'open': 80., 'low': 70.}]
    assert stop_loss_result(bars, date(2026, 8, 3), 100, 10, -100, .15)['hit']
    assert not stop_loss_result(bars, date(2026, 8, 3), 100, 10, -100, .75)['hit']
    bars.append({'date': '2026-08-05', 'open': 20., 'low': 18.})
    result = stop_loss_result(bars, date(2026, 8, 3), 100, 10, -100, .75)
    assert result['stop_price'] == 25
    assert result['exit_price'] == 20
    assert result['pl'] == -800


def test_stop_hit_and_gap_fill():
    bars = [{"date": "2026-08-04", "open": 80., "low": 78., "high": 90.}]
    result = stop_loss_result(bars, date(2026, 8, 3), 100, 10, 100)
    assert result["hit"]
    assert result["exit_price"] == 80
    assert result["pl"] == -200
    assert result["delta"] == -300


def test_stop_touch_fills_at_stop():
    result = stop_loss_result([{"date": "2026-08-04", "open": 90., "low": 85.}], date(2026, 8, 3), 100, 10, -250)
    assert result["pl"] == -150
    assert result["delta"] == 100


def test_no_hit_retains_actual_and_ignores_purchase_day():
    result = stop_loss_result([
        {"date": "2026-08-03", "open": 80., "low": 70.},
        {"date": "2026-08-04", "open": 100., "low": 95.},
    ], date(2026, 8, 3), 100, 10, 250)
    assert not result["hit"]
    assert result["pl"] == 250
    assert result["delta"] == 0


def test_missing_low_is_excluded():
    result = stop_loss_result([{"date": "2026-08-04", "high": 120.}], date(2026, 8, 3), 100, 10, 250)
    assert not result["available"]
    assert result["pl"] is None


def test_history_returns_copy_not_cached_list():
    start, end = date(2026, 8, 3), date(2026, 8, 4)
    cache = {cache_key("AAA", start, end): {"highs": [{"date": "2026-08-04", "high": 100}]}}
    bars = history_highs("AAA", start, end, cache=cache)
    bars.append({"date": "2026-08-04", "high": 110})
    assert len(cache[cache_key("AAA", start, end)]["highs"]) == 1
