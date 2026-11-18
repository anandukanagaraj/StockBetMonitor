import sqlite3

import pytest

import db
import scheduler


def test_unreconciled_consolidation_does_not_generate_target_alert():
    assert scheduler.evaluate({
        "ticker": "CCAVENUE", "bet_no": 80, "current_price": None,
        "buy_price": 17.12, "target_price": 18.90, "qty": 583,
        "stop_loss": None,
    }) is None


def test_held_position_rejects_quote_but_other_positions_refresh(tmp_path, monkeypatch):
    path = tmp_path / "review.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE positions (id INTEGER PRIMARY KEY, current_price REAL, updated_at TEXT);
            CREATE TABLE position_price_review_holds (position_id INTEGER PRIMARY KEY, reason TEXT);
            INSERT INTO positions VALUES (87, NULL, NULL), (88, 100, NULL);
            INSERT INTO position_price_review_holds VALUES (87, '10-to-1 consolidation');
        """)
    with pytest.raises(ValueError, match="holdings review"):
        db.update_current_price(87, 138.50)
    db.update_current_price(88, 101)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT current_price FROM positions WHERE id=87").fetchone()[0] is None
        assert connection.execute("SELECT current_price FROM positions WHERE id=88").fetchone()[0] == 101
        connection.execute("DELETE FROM position_price_review_holds WHERE position_id=87")
    db.update_current_price(87, 138.50)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT current_price FROM positions WHERE id=87").fetchone()[0] == 138.50
