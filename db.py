import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock_monitor.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_no INTEGER UNIQUE,
    ticker TEXT NOT NULL,
    buy_date TEXT NOT NULL,
    qty REAL NOT NULL,
    buy_price REAL NOT NULL,
    target_price REAL,
    stop_loss REAL,
    current_price REAL,
    sell_date TEXT,
    sell_price REAL,
    status TEXT NOT NULL DEFAULT 'Open',
    notes TEXT,
    is_test_data INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with connect() as connection:
        connection.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(positions)").fetchall()
        }
        if "is_test_data" not in columns:
            connection.execute("ALTER TABLE positions ADD COLUMN is_test_data INTEGER NOT NULL DEFAULT 0")


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def next_bet_no():
    with connect() as connection:
        value = connection.execute("SELECT COALESCE(MAX(bet_no), 0) + 1 FROM positions").fetchone()[0]
        return int(value)


def list_positions():
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM positions
            ORDER BY
                CASE
                    WHEN sell_price IS NULL AND sell_date IS NULL THEN 0
                    ELSE 1
                END,
                buy_date DESC,
                id DESC
            """
        ).fetchall()


def list_positions_for_management():
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM positions
            WHERE sell_price IS NULL
              AND sell_date IS NULL
              AND LOWER(COALESCE(status, '')) NOT IN ('closed', 'target hit', 'stop loss hit', 'sold')
            ORDER BY
                COALESCE(created_at, updated_at) DESC,
                id DESC
            """
        ).fetchall()


def list_test_positions():
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM positions
            WHERE COALESCE(is_test_data, 0) = 1
               OR UPPER(COALESCE(ticker, '')) = 'TEST'
            ORDER BY
                COALESCE(updated_at, created_at) DESC,
                id DESC
            """
        ).fetchall()


def get_position(position_id):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()


def delete_position(position_id):
    with connect() as connection:
        connection.execute("DELETE FROM positions WHERE id = ?", (position_id,))


def create_position(data):
    timestamp = now_iso()
    bet_no = data.get("bet_no") or next_bet_no()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO positions (
                bet_no, ticker, buy_date, qty, buy_price, target_price, stop_loss,
                current_price, sell_date, sell_price, status, notes, is_test_data, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bet_no,
                data["ticker"].strip().upper(),
                data["buy_date"],
                data["qty"],
                data["buy_price"],
                data.get("target_price"),
                data.get("stop_loss"),
                data.get("current_price"),
                data.get("sell_date"),
                data.get("sell_price"),
                data.get("status") or "Open",
                data.get("notes"),
                1 if data.get("is_test_data") else 0,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid


def update_position(position_id, data):
    with connect() as connection:
        connection.execute(
            """
            UPDATE positions
            SET bet_no = ?,
                ticker = ?,
                buy_date = ?,
                qty = ?,
                buy_price = ?,
                target_price = ?,
                stop_loss = ?,
                current_price = ?,
                sell_date = ?,
                sell_price = ?,
                status = ?,
                notes = ?,
                is_test_data = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                data.get("bet_no"),
                data["ticker"].strip().upper(),
                data["buy_date"],
                data["qty"],
                data["buy_price"],
                data.get("target_price"),
                data.get("stop_loss"),
                data.get("current_price"),
                data.get("sell_date"),
                data.get("sell_price"),
                data.get("status") or "Open",
                data.get("notes"),
                1 if data.get("is_test_data") else 0,
                now_iso(),
                position_id,
            ),
        )


def update_current_price(position_id, price):
    with connect() as connection:
        connection.execute(
            "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
            (price, now_iso(), position_id),
        )


def is_closed(row):
    status = (row["status"] or "").strip().lower()
    closed_statuses = {"closed", "target hit", "stop loss hit", "sold"}
    return row["sell_price"] is not None or row["sell_date"] is not None or status in closed_statuses


def metrics(positions):
    open_positions = [row for row in positions if not is_closed(row)]
    closed_positions = [row for row in positions if is_closed(row)]
    realized_profit = sum(
        ((row["sell_price"] or 0) - row["buy_price"]) * row["qty"]
        for row in closed_positions
        if row["sell_price"] is not None
    )
    total_invested = sum(row["buy_price"] * row["qty"] for row in positions)
    closed_with_sell = [row for row in closed_positions if row["sell_price"] is not None]
    winners = [
        row for row in closed_with_sell
        if ((row["sell_price"] or 0) - row["buy_price"]) * row["qty"] > 0
    ]
    win_rate = (len(winners) / len(closed_with_sell) * 100) if closed_with_sell else 0
    return {
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "total_invested": total_invested,
        "realized_profit": realized_profit,
        "win_rate": win_rate,
    }
