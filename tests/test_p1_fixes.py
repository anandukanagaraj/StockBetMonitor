"""Tests for the P1 review fixes: alert de-duplication, email fallback,
consistent backups, bootstrap admin password, back-dated fund pricing."""
import importlib
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import db
import options_db
import scheduler
from services import db_backup


# ---------------------------------------------------------------- alerts

TODAY = "2026-11-26"


@pytest.mark.parametrize(
    "previous, status, expected",
    [
        (None, "HOLD", False),
        (None, "NEAR TARGET", True),
        ({"status": "NEAR TARGET", "sent_date": TODAY}, "NEAR TARGET", False),
        ({"status": "NEAR TARGET", "sent_date": TODAY}, "VERY CLOSE", True),
        ({"status": "VERY CLOSE", "sent_date": TODAY}, "TARGET HIT", True),
        # Dropping back a level is silent; climbing again the same day is not re-sent.
        ({"status": "VERY CLOSE", "sent_date": TODAY}, "NEAR TARGET", False),
        ({"status": "NEAR TARGET", "sent_date": TODAY}, "NEAR TARGET", False),
        # Back to HOLD then up again: alert again.
        ({"status": "HOLD", "sent_date": TODAY}, "NEAR TARGET", True),
        ({"status": "STOP LOSS HIT", "sent_date": TODAY}, "STOP LOSS HIT", False),
        ({"status": "TARGET HIT", "sent_date": TODAY}, "STOP LOSS HIT", True),
        # One reminder on a new day.
        ({"status": "TARGET HIT", "sent_date": "2026-11-25"}, "TARGET HIT", True),
    ],
)
def test_should_send_alert(previous, status, expected):
    assert scheduler.should_send_alert(previous, status, TODAY) is expected


def test_hold_keeps_last_sent_date_so_reentry_same_day_alerts():
    previous = {"status": "NEAR TARGET", "sent_date": TODAY}
    state = scheduler.next_alert_state(previous, "HOLD", TODAY, sent=False)
    assert state == {"status": "HOLD", "sent_date": TODAY}
    assert scheduler.should_send_alert(state, "NEAR TARGET", TODAY) is True


def _row(position_id, current, target=100, buy=80, stop=None):
    return {"id": position_id, "ticker": f"T{position_id}", "bet_no": position_id,
            "current_price": current, "target_price": target, "buy_price": buy,
            "qty": 1, "stop_loss": stop}


def test_market_check_sends_each_alert_once(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(scheduler, "send_alert", lambda config, result, message: sent.append(result.ticker))
    monkeypatch.setattr(scheduler, "record_portfolio_snapshot", lambda: None)
    monkeypatch.setattr(scheduler, "generate_report_snapshots", lambda config: None)
    monkeypatch.setattr(scheduler, "run_stock_price_alert_check", lambda config, known_prices: None)
    monkeypatch.setattr(scheduler, "write_heartbeat", lambda **kwargs: None)
    rows = [_row(1, 99), _row(2, 50)]  # 1 is VERY CLOSE, 2 is HOLD
    monkeypatch.setattr(scheduler, "refresh_open_position_prices", lambda config, context: (rows, []))

    scheduler.run_market_check(None)
    scheduler.run_market_check(None)
    scheduler.run_market_check(None)
    assert sent == ["T1"]

    rows[0] = _row(1, 101)  # now TARGET HIT
    scheduler.run_market_check(None)
    assert sent == ["T1", "T1"]


def test_alert_email_only_when_telegram_fails(monkeypatch):
    emails = []
    monkeypatch.setattr(scheduler, "send_whatsapp", lambda config, message: False)
    monkeypatch.setattr(scheduler, "format_telegram_message", lambda result: "msg")
    monkeypatch.setattr(scheduler, "send_eod_email", lambda config, subject, body: emails.append(subject) or True)
    result = SimpleNamespace(status="TARGET HIT", ticker="ABC", bet_no=1)

    monkeypatch.setattr(scheduler, "send_telegram", lambda config, message: True)
    assert scheduler.send_alert(None, result, "m")["email"] is False
    assert emails == []

    monkeypatch.setattr(scheduler, "send_telegram", lambda config, message: False)
    assert scheduler.send_alert(None, result, "m")["email"] is True
    assert len(emails) == 1


# ---------------------------------------------------------------- backups

def test_backup_includes_wal_changes_and_keeps_milestones(tmp_path):
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (v INTEGER)")
    writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    writer.commit()  # rows live in the WAL file, not yet in live.db
    assert (tmp_path / "live.db-wal").stat().st_size > 0

    backups = tmp_path / "backups"
    backups.mkdir()
    milestone = backups / "stock_monitor_before_migration.db"
    milestone.write_bytes(b"keep me")
    for day in range(1, 4):
        db_backup.backup_database(source, backups, keep=2, now=datetime(2026, 11, day, 16, 0, 0))
    writer.close()

    daily = sorted(path.name for path in backups.glob("stock_monitor_daily_*.db"))
    assert daily == ["stock_monitor_daily_20261102_160000.db", "stock_monitor_daily_20261103_160000.db"]
    assert milestone.exists()
    with sqlite3.connect(backups / daily[-1]) as copy:
        assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 500


def test_backup_copies_off_site(tmp_path):
    source = tmp_path / "live.db"
    sqlite3.connect(source).close()
    mirror = tmp_path / "cloud"
    path = db_backup.backup_database(source, tmp_path / "b", copy_dir=mirror)
    assert (mirror / path.name).exists()


def test_daily_backup_runs_once_after_report_time(tmp_path, monkeypatch):
    import configparser

    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(scheduler, "backup_database", lambda keep, copy_dir: calls.append(keep) or tmp_path / "x.db")
    config = configparser.ConfigParser()
    config.read_dict({"MARKET": {"REPORT_TIME": "15:35"}, "BACKUP": {"KEEP": "7"}})

    assert scheduler.run_daily_backup_if_due(config, now=datetime(2026, 11, 26, 10, 0)) is None
    scheduler.run_daily_backup_if_due(config, now=datetime(2026, 11, 26, 15, 40))
    scheduler.run_daily_backup_if_due(config, now=datetime(2026, 11, 26, 16, 40))
    assert calls == [7]
    scheduler.run_daily_backup_if_due(config, now=datetime(2026, 11, 27, 15, 36))
    assert calls == [7, 7]


# ---------------------------------------------------------------- login

@pytest.fixture
def web_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(options_db, "DB_PATH", tmp_path / "app.db")
    app_module = importlib.import_module("app")
    app_module.app.config["TESTING"] = True
    return app_module


def _login(app_module, username, password):
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "t"
    response = client.post("/login", data={"username": username, "password": password, "csrf_token": "t"})
    with client.session_transaction() as sess:
        return response, dict(sess)


def _admin(password_hash, is_active=1):
    return {"id": 1, "username": "admin", "password_hash": password_hash, "is_admin": 1, "is_active": is_active}


def test_bootstrap_password_rejected_once_admin_has_own_password(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    monkeypatch.setattr(web_app, "ACTIVE_PASSWORD_HASH", generate_password_hash("bootstrap-pass"))
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: _admin(generate_password_hash("my-own-password")))
    response, sess = _login(web_app, "admin", "bootstrap-pass")
    assert "user_id" not in sess
    response, sess = _login(web_app, "admin", "my-own-password")
    assert sess.get("user_id") == 1


def test_bootstrap_password_works_for_first_login_only(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    stored = {}
    monkeypatch.setattr(web_app, "ACTIVE_PASSWORD_HASH", generate_password_hash("bootstrap-pass"))
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: _admin(""))
    monkeypatch.setattr(web_app, "set_user_password", lambda user_id, value: stored.update({user_id: value}))
    response, sess = _login(web_app, "admin", "bootstrap-pass")
    assert sess.get("user_id") == 1
    assert 1 in stored


def test_disabled_admin_cannot_use_bootstrap_password(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    monkeypatch.setattr(web_app, "ACTIVE_PASSWORD_HASH", generate_password_hash("bootstrap-pass"))
    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: _admin("", is_active=0))
    response, sess = _login(web_app, "admin", "bootstrap-pass")
    assert "user_id" not in sess


def test_default_password_flags_change_locally_and_is_refused_when_hosted(web_app, monkeypatch):
    from werkzeug.security import generate_password_hash

    monkeypatch.setattr(web_app, "get_user_by_username", lambda name: _admin(generate_password_hash("admin123")))
    response, sess = _login(web_app, "admin", "admin123")
    assert sess.get("must_change_password") is True

    monkeypatch.setattr(web_app, "IS_LOCAL_BIND", False)
    response, sess = _login(web_app, "admin", "admin123")
    assert "user_id" not in sess


# ---------------------------------------------------------------- fund pricing

def test_backdated_transaction_uses_nav_of_that_date(tmp_path, monkeypatch):
    import fund_management

    path = tmp_path / "fund.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fund_nav_snapshots (fund_id INTEGER, snapshot_date TEXT, nav REAL)")
        connection.executemany(
            "INSERT INTO fund_nav_snapshots VALUES (?, ?, ?)",
            [(1, "2026-11-20", 11.0), (1, "2026-11-24", 12.5)],
        )
    monkeypatch.setattr(db, "DB_PATH", path)
    today = date(2026, 11, 26)
    nav_for = fund_management._nav_for_transaction

    assert nav_for(1, 13.0, "", today=today) == ("2026-11-26", 13.0, "2026-11-26")
    assert nav_for(1, 13.0, "2026-11-26", today=today) == ("2026-11-26", 13.0, "2026-11-26")
    assert nav_for(1, 13.0, "2026-11-24", today=today) == ("2026-11-24", 12.5, "2026-11-24")
    # Closest earlier NAV when the exact day has no snapshot.
    assert nav_for(1, 13.0, "2026-11-22", today=today) == ("2026-11-22", 11.0, "2026-11-20")
    with pytest.raises(ValueError, match="future"):
        nav_for(1, 13.0, "2026-11-27", today=today)
    with pytest.raises(ValueError, match="No NAV"):
        nav_for(1, 13.0, "2026-11-01", today=today)
    with pytest.raises(ValueError, match="valid"):
        nav_for(1, 13.0, "26/11/2026", today=today)
