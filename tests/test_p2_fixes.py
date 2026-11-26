"""Tests for the P2 review fixes."""
import importlib
import json
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

import db
import options_db
import scheduler
from services import option_charges
from services.json_cache import atomic_write_json, prune_superseded


@pytest.fixture
def web_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(options_db, "DB_PATH", tmp_path / "app.db")
    app_module = importlib.import_module("app")
    app_module.app.config["TESTING"] = True
    app_module._login_failures.clear()
    return app_module


def _client(app_module, **session_values):
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "t"
        sess.update(session_values)
    return client


# ---------------------------------------------------------------- security

@pytest.mark.parametrize(
    "value, expected",
    [
        ("/options", "/options"),
        ("/report?x=1", "/report?x=1"),
        ("https://evil.example", None),
        ("//evil.example", None),
        ("/\\evil.example", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_next_url(web_app, value, expected):
    assert web_app.safe_next_url(value) == expected


def test_login_redirect_ignores_external_next(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    user = {"id": 1, "username": "admin", "password_hash": generate_password_hash("correct-password"), "is_admin": 1, "is_active": 1}
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: user)
    client = _client(web_app)
    response = client.post("/login?next=https://evil.example",
                           data={"username": "admin", "password": "correct-password", "csrf_token": "t"})
    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


def test_login_locks_after_repeated_failures(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    user = {"id": 1, "username": "admin", "password_hash": generate_password_hash("correct-password"), "is_admin": 1, "is_active": 1}
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: user)
    client = _client(web_app)
    for _ in range(web_app.LOGIN_MAX_FAILURES):
        client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": "t"})
    client.post("/login", data={"username": "admin", "password": "correct-password", "csrf_token": "t"})
    with client.session_transaction() as sess:
        assert "user_id" not in sess  # locked even with the right password


def test_lockout_expires(web_app):
    with web_app.app.test_request_context("/login"):
        for _ in range(web_app.LOGIN_MAX_FAILURES):
            web_app.record_login_failure("admin", now=1000)
        assert web_app.login_locked("admin", now=1001)
        assert not web_app.login_locked("admin", now=1000 + web_app.LOGIN_LOCK_SECONDS + 1)


def test_disabled_user_session_is_ended(web_app, monkeypatch):
    monkeypatch.setattr(web_app, "get_user_by_id", lambda user_id: {"id": 5, "is_active": 0, "is_admin": 0})
    client = _client(web_app, user_id=5, username="bob", is_admin=False)
    response = client.get("/options")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_admin_flag_refreshed_from_database(web_app, monkeypatch):
    monkeypatch.setattr(web_app, "get_user_by_id", lambda user_id: {"id": 5, "is_active": 1, "is_admin": 0})
    client = _client(web_app, user_id=5, username="bob", is_admin=True)
    assert client.get("/users").status_code == 403


def test_api_requires_key_when_hosted_even_from_localhost(web_app, monkeypatch):
    monkeypatch.setattr(web_app, "API_KEY", "")
    monkeypatch.setattr(web_app, "IS_LOCAL_BIND", False)
    assert web_app.app.test_client().get("/api/portfolio/actual-pl").status_code == 401


def test_api_returns_only_the_configured_users_data(web_app, monkeypatch):
    from user_context import get_current_user_id

    monkeypatch.setattr(web_app, "API_KEY", "k")
    monkeypatch.setattr(web_app, "API_USERNAME", "alice")
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: {"id": 42} if name == "alice" else None)
    monkeypatch.setattr(web_app, "build_portfolio_api_payload", lambda: {"user": get_current_user_id()})
    response = web_app.app.test_client().get("/api/portfolio/actual-pl", headers={"X-API-Key": "k"})
    assert response.get_json() == {"user": 42}


# ---------------------------------------------------------------- alerts

def test_stop_loss_alerts_can_be_disabled():
    row = {"id": 1, "ticker": "X", "bet_no": 1, "current_price": 90, "target_price": 150,
           "buy_price": 100, "qty": 1, "stop_loss": 95}
    assert scheduler.evaluate(row).status == "STOP LOSS HIT"
    assert scheduler.evaluate(row, stop_loss_enabled=False).status == "HOLD"


def test_failed_price_alert_sent_once_per_day_per_list():
    rows = [{"ticker": "abc"}, {"ticker": "XYZ"}]
    tickers, message = scheduler.failed_price_alert_message(rows, None, "2026-11-26")
    assert tickers == ["ABC", "XYZ"] and "ABC, XYZ" in message
    previous = {"date": "2026-11-26", "tickers": ["ABC", "XYZ"]}
    assert scheduler.failed_price_alert_message(rows, previous, "2026-11-26") is None
    assert scheduler.failed_price_alert_message(rows[:1], previous, "2026-11-26") is not None
    assert scheduler.failed_price_alert_message(rows, previous, "2026-11-27") is not None
    assert scheduler.failed_price_alert_message([], None, "2026-11-26") is None


# ---------------------------------------------------------------- funds

def _position(position_id, ticker, buy_date, created_at, sell_price=None, sell_date=None, qty=10, buy_price=100):
    return {"id": position_id, "ticker": ticker, "buy_date": buy_date, "qty": qty, "buy_price": buy_price,
            "created_at": created_at, "sell_price": sell_price, "sell_date": sell_date,
            "status": "Closed" if sell_price is not None else "Open"}


@pytest.fixture
def fund_db(tmp_path, monkeypatch):
    path = tmp_path / "fund.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE fund_position_baselines (fund_id INTEGER, position_id INTEGER, created_at TEXT,"
            " ticker TEXT, buy_date TEXT, qty REAL, buy_price REAL)"
        )
        connection.execute("INSERT INTO fund_position_baselines VALUES (1, 10, 'x', 'BASE', '2026-07-01', 10, 100)")
    monkeypatch.setattr(db, "DB_PATH", path)
    return {"id": 1, "created_at": "2026-08-15T10:00:00"}


def test_reentered_baseline_holding_is_not_a_purchase(fund_db):
    import fund_management as fm

    original = [_position(10, "BASE", "2026-07-01", "2026-07-01T09:00:00")]
    reentered = [_position(99, "base", "2026-07-01", "2026-11-26T12:00:00")]  # new id, typed in later
    assert fm._automatic_stock_cash_flows(fund_db, original)["net_cash"] == 0
    assert fm._automatic_stock_cash_flows(fund_db, reentered)["net_cash"] == 0


def test_pre_fund_round_trip_entered_later_is_ignored(fund_db):
    import fund_management as fm

    historical = [_position(50, "OLD", "2026-05-01", "2026-11-26T12:00:00", sell_price=120, sell_date="2026-06-01")]
    flows = fm._automatic_stock_cash_flows(fund_db, historical)
    assert flows["purchase_count"] == 0 and flows["net_cash"] == 0


def test_post_inception_trades_still_move_cash(fund_db):
    import fund_management as fm

    rows = [
        _position(60, "NEW", "2026-09-01", "2026-09-01T10:00:00"),
        _position(61, "FLIP", "2026-09-02", "2026-09-02T10:00:00", sell_price=110, sell_date="2026-09-20"),
        _position(10, "BASE", "2026-07-01", "2026-07-01T09:00:00", sell_price=130, sell_date="2026-10-01"),
    ]
    flows = fm._automatic_stock_cash_flows(fund_db, rows)
    assert flows["purchases"] == 2000  # NEW + FLIP
    assert flows["sale_proceeds"] == 1100 + 1300  # FLIP + baseline sale
    assert flows["net_cash"] == 400


# ---------------------------------------------------------------- form validation

def _form(**overrides):
    values = {"ticker": "reliance", "buy_date": "2026-11-01", "qty": "10", "buy_price": "100", "target_price": "120"}
    values.update(overrides)
    return MultiDict(values)


def test_position_form_normalises_valid_input(web_app):
    data = web_app.position_from_form(_form(sell_date="2026-11-20", sell_price="110", status="Closed"))
    assert data["ticker"] == "RELIANCE" and data["buy_date"] == "2026-11-01" and data["sell_date"] == "2026-11-20"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"ticker": ""}, "Ticker is required"),
        ({"ticker": "BAD<script>"}, "Ticker may only"),
        ({"buy_date": ""}, "Buy Date is required"),
        ({"buy_date": "01/11/2026"}, "valid date"),
        ({"sell_date": "2026-10-01", "sell_price": "110"}, "cannot be before"),
    ],
)
def test_position_form_rejects_bad_input(web_app, overrides, message):
    with pytest.raises(ValueError, match=message):
        web_app.position_from_form(_form(**overrides))


def test_tickers_with_symbols_are_allowed(web_app):
    assert web_app.position_from_form(_form(ticker="M&M"))["ticker"] == "M&M"
    assert web_app.position_from_form(_form(ticker="BAJAJ-AUTO"))["ticker"] == "BAJAJ-AUTO"


# ---------------------------------------------------------------- caches

def test_prune_keeps_newest_end_date_per_ticker_and_start():
    cache = {
        "v1|ABC|2026-08-01|2026-11-24": 1,
        "v1|ABC|2026-08-01|2026-11-26": 2,
        "v1|ABC|2026-09-01|2026-10-01": 3,
        "v1|XYZ|2026-08-01|2026-11-25": 4,
        "old|ABC|2026-08-01|2026-11-26": 5,
        "malformed": 6,
    }
    assert prune_superseded(cache, "v1") == {
        "v1|ABC|2026-08-01|2026-11-26": 2,
        "v1|ABC|2026-09-01|2026-10-01": 3,
        "v1|XYZ|2026-08-01|2026-11-25": 4,
    }


def test_atomic_write_json_replaces_file_and_leaves_no_temp(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("old", encoding="utf-8")
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]


# ---------------------------------------------------------------- charges

def test_charge_rates_can_be_overridden_from_config(tmp_path, monkeypatch):
    for name in option_charges.CHARGE_SETTINGS:
        monkeypatch.setattr(option_charges, name, getattr(option_charges, name))
    config = tmp_path / "config.ini"
    config.write_text("[CHARGES]\nOPTION_STT_RATE = 0.002\nGST_RATE = bad\nSEBI_RATE = -1\n", encoding="utf-8")
    applied = option_charges.load_charge_overrides(config)
    assert applied == {"OPTION_STT_RATE": 0.002}
    assert option_charges.OPTION_STT_RATE == 0.002
    assert option_charges.GST_RATE == 0.18
