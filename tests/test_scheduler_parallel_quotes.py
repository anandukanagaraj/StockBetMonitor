import configparser

import pandas as pd

import scheduler
from services import yahoo_service


def scheduler_config(workers=4):
    config = configparser.ConfigParser()
    config["SCHEDULER"] = {"PRICE_FETCH_WORKERS": str(workers)}
    return config


def test_fetch_latest_prices_uses_bulk_then_falls_back(monkeypatch):
    fallback_calls = []

    monkeypatch.setattr(scheduler, "latest_prices", lambda tickers: {"AAA": 101.0})

    def fake_latest_price(ticker):
        fallback_calls.append(ticker)
        return 202.0

    monkeypatch.setattr(scheduler, "latest_price", fake_latest_price)

    prices, failures = scheduler.fetch_latest_prices(
        ["AAA", "aaa", "BBB"],
        scheduler_config(),
        context="test",
    )

    assert prices == {"AAA": 101.0, "BBB": 202.0}
    assert failures == {}
    assert fallback_calls == ["BBB"]


def test_yahoo_bulk_prices_maps_multi_symbol_response(monkeypatch):
    yahoo_service._PRICE_CACHE.clear()
    columns = pd.MultiIndex.from_tuples(
        [("AAA.NS", "Close"), ("BBB.NS", "Close")]
    )
    response = pd.DataFrame(
        [[100.0, 200.0], [101.5, 202.25]],
        columns=columns,
    )
    monkeypatch.setattr(yahoo_service.yf, "download", lambda **kwargs: response)

    prices = yahoo_service.latest_prices(["AAA", "BBB"])

    assert prices == {"AAA": 101.5, "BBB": 202.25}


def test_price_fetch_worker_limit_is_bounded():
    assert scheduler.price_fetch_workers(scheduler_config(100), 50) == 16
    assert scheduler.price_fetch_workers(scheduler_config(8), 3) == 3
    assert scheduler.price_fetch_workers(scheduler_config(0), 3) == 1
