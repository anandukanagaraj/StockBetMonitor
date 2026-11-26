"""Web-layer tests: every page loads, needs login, POSTs need CSRF, and users
can't see or change each other's records. Also covers schema versioning."""
import threading

import pytest

import db
import options_db
from conftest import make_client
from user_context import reset_current_user_id, set_current_user_id

PUBLIC_ENDPOINTS = {"static", "health", "login", "logout", "api_portfolio_actual_pl"}


def _as_user(user_id):
    return set_current_user_id(user_id)


def seed_portfolio(user_id=1):
    """Two stock bets (open + closed) and one closed option strategy for user_id."""
    token = _as_user(user_id)
    try:
        group = db.get_default_stock_trade_group()
        base = dict(trade_group_id=group["id"], target_price=120, stop_loss=90, notes=None, is_test_data=False)
        open_id = db.create_position({**base, "ticker": "AAA", "buy_date": "2026-09-01", "qty": 10,
                                      "buy_price": 100, "current_price": 110, "status": "Open"})
        closed_id = db.create_position({**base, "ticker": "BBB", "buy_date": "2026-08-01", "qty": 5,
                                        "buy_price": 200, "current_price": None, "sell_date": "2026-09-15",
                                        "sell_price": 230, "status": "Closed"})
        option_group = options_db.list_trade_groups(active_only=True)[0]
        strategy_id = options_db.create_option_strategy(
            {"trade_group_id": option_group["id"], "strategy_type": "Bull Call Spread", "underlying": "NIFTY",
             "buy_date": "2026-09-01", "sell_date": "2026-09-20", "expiry_date": "2026-09-25",
             "status": "Closed", "stock_move_pct": 2.5},
            [
                {"ticker": "NIFTY 25SEP 24000 CE", "action": "BUY", "qty": 75, "buy_price": 120, "sell_price": 180},
                {"ticker": "NIFTY 25SEP 24200 CE", "action": "SELL", "qty": 75, "buy_price": 60, "sell_price": 80},
            ],
        )
        return {"open_id": open_id, "closed_id": closed_id, "strategy_id": strategy_id}
    finally:
        reset_current_user_id(token)


def get_pages(app_module):
    return sorted(
        rule.rule for rule in app_module.app.url_map.iter_rules()
        if "GET" in rule.methods and not rule.arguments and rule.endpoint not in PUBLIC_ENDPOINTS
    )


def post_rules(app_module):
    return sorted(
        (rule.rule, rule.endpoint) for rule in app_module.app.url_map.iter_rules()
        if "POST" in rule.methods and rule.endpoint not in {"static"}
    )


# ---------------------------------------------------------------- every page renders

def test_every_page_renders_with_data(app_db):
    ids = seed_portfolio()
    client = make_client(app_db)
    failures = {}
    for page in get_pages(app_db):
        response = client.get(page)
        if response.status_code not in (200, 302):
            failures[page] = response.status_code
    for page in (f"/positions/{ids['open_id']}/edit", f"/options/{ids['strategy_id']}",
                 f"/options/{ids['strategy_id']}/edit"):
        response = client.get(page)
        if response.status_code != 200:
            failures[page] = response.status_code
    assert failures == {}


def test_pages_show_calculated_figures(app_db, monkeypatch):
    seed_portfolio()
    monkeypatch.setattr(app_db, "API_KEY", "")
    monkeypatch.setattr(app_db, "IS_LOCAL_BIND", True)
    client = make_client(app_db)
    api = app_db.app.test_client().get("/api/portfolio/actual-pl")  # localhost, no key on a local install
    payload = api.get_json()
    assert payload["stocks"]["summary"]["realized_pl"] == 150  # (230 - 200) x 5
    assert payload["stocks"]["summary"]["unrealized_pl"] == 100  # (110 - 100) x 10
    assert payload["options"]["summary"]["realized_pl"] == 3000  # (180-120)x75 + (60-80)x75
    assert b"NIFTY" in client.get("/options").data


# ---------------------------------------------------------------- login and CSRF

def test_every_page_requires_login(app_db):
    client = make_client(app_db, user_id=None)
    for page in get_pages(app_db):
        response = client.get(page)
        assert response.status_code == 302, page
        assert "/login" in response.headers["Location"], page


def test_every_post_requires_csrf_token(app_db):
    client = app_db.app.test_client()
    with client.session_transaction() as sess:
        sess.update(user_id=1, username="admin", is_admin=True, csrf_token="real-token")
    for rule, endpoint in post_rules(app_db):
        url = rule
        for argument in ("<int:position_id>", "<int:strategy_id>", "<int:group_id>", "<int:alert_id>",
                         "<int:user_id>", "<int:fund_id>", "<int:investor_id>", "<int:profile_id>"):
            url = url.replace(argument, "1")
        response = client.post(url, data={"csrf_token": "wrong"})
        assert response.status_code == 400, (rule, response.status_code)


def test_admin_pages_forbidden_for_normal_users(app_db, monkeypatch):
    monkeypatch.setattr(app_db, "get_user_by_id", lambda user_id: {"id": user_id, "is_active": 1, "is_admin": 0})
    client = make_client(app_db, user_id=2, username="bob", is_admin=False)
    assert client.get("/users").status_code == 403


# ---------------------------------------------------------------- user isolation

def _second_user(app_db):
    from werkzeug.security import generate_password_hash

    user_id = db.create_user("bob", generate_password_hash("bob-password-1"), "Bob", "bob@example.com", "", "")
    token = _as_user(user_id)
    try:
        db.create_stock_trade_group("Bob stocks", "BOB1", "")
        options_db.create_trade_group("Bob options", "BOBO1", "")
    finally:
        reset_current_user_id(token)
    return user_id


def test_users_cannot_see_or_change_each_others_bets(app_db):
    ids = seed_portfolio(user_id=1)
    bob = _second_user(app_db)
    client = make_client(app_db, user_id=bob, username="bob", is_admin=False)

    edit = client.get(f"/positions/{ids['open_id']}/edit")
    assert edit.status_code == 302  # "Bet not found"
    client.post(f"/positions/{ids['open_id']}/delete", data={"csrf_token": "test-token"})
    token = _as_user(1)
    try:
        assert db.get_position(ids["open_id"]) is not None  # still there
    finally:
        reset_current_user_id(token)
    assert b"AAA" not in client.get("/add-bet").data
    assert client.get(f"/options/{ids['strategy_id']}").status_code in (302, 404)


def test_users_cannot_delete_each_others_option_strategies(app_db):
    ids = seed_portfolio(user_id=1)
    bob = _second_user(app_db)
    client = make_client(app_db, user_id=bob, username="bob", is_admin=False)
    client.post(f"/options/{ids['strategy_id']}/delete", data={"csrf_token": "test-token"})
    token = _as_user(1)
    try:
        strategy, _legs = options_db.get_option_strategy(ids["strategy_id"])
        assert strategy is not None
    finally:
        reset_current_user_id(token)


def test_bets_cannot_be_moved_into_another_users_group(app_db):
    ids = seed_portfolio(user_id=1)
    bob = _second_user(app_db)
    token = _as_user(1)
    try:
        alice_group = db.get_default_stock_trade_group()["id"]
    finally:
        reset_current_user_id(token)
    client = make_client(app_db, user_id=bob, username="bob", is_admin=False)
    client.post("/positions/new", data={
        "csrf_token": "test-token", "trade_group_id": alice_group, "ticker": "ZZZ", "buy_date": "2026-11-01",
        "qty": "1", "buy_price": "10", "target_price": "12",
    })
    token = _as_user(1)
    try:
        assert all(row["ticker"] != "ZZZ" for row in db.list_positions(trade_group_code=None))
    finally:
        reset_current_user_id(token)


# ---------------------------------------------------------------- schema versioning

def test_schema_version_recorded_and_reused(app_db, monkeypatch):
    from services import schema

    with db.connect() as connection:
        version = connection.execute("SELECT version FROM app_schema_version").fetchone()["version"]
    assert version == schema.SCHEMA_VERSION

    calls = []
    monkeypatch.setattr(schema, "_run_all_migrations", lambda: calls.append(1))
    schema._ensured.clear()
    assert schema.ensure_schema() is False  # version matches: nothing to do
    monkeypatch.setattr(schema, "SCHEMA_VERSION", "next-version")
    schema._ensured.clear()
    assert schema.ensure_schema() is True
    assert calls == [1]


def test_concurrent_ensure_schema_runs_migrations_once(tmp_path, monkeypatch):
    from services import schema

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "race.db")
    schema._ensured.clear()
    runs = []
    original = schema._run_all_migrations

    def slow_migrations():
        runs.append(1)
        original()

    monkeypatch.setattr(schema, "_run_all_migrations", slow_migrations)
    threads = [threading.Thread(target=schema.ensure_schema) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert runs == [1]
