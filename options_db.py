import re
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock_monitor.db"


OPTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS option_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_no INTEGER UNIQUE,
    bet_no INTEGER,
    strategy_type TEXT NOT NULL,
    underlying TEXT NOT NULL,
    buy_date TEXT NOT NULL,
    sell_date TEXT,
    expiry_date TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    margin REAL,
    max_risk REAL,
    max_profit REAL,
    strategy_pl REAL,
    pl_pct REAL,
    stock_move_pct REAL,
    long_only_pl REAL,
    notes TEXT,
    is_test_data INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS option_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    leg_no INTEGER NOT NULL,
    action TEXT NOT NULL DEFAULT 'BUY',
    ticker TEXT NOT NULL,
    underlying TEXT,
    expiry_text TEXT,
    strike REAL,
    option_type TEXT,
    qty REAL NOT NULL,
    buy_price REAL NOT NULL,
    trade_capital REAL,
    sell_price REAL,
    achieved_pl REAL,
    status TEXT NOT NULL DEFAULT 'Open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(strategy_id) REFERENCES option_strategies(id) ON DELETE CASCADE
);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def init_options_db():
    with connect() as connection:
        connection.executescript(OPTION_SCHEMA)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(option_strategies)").fetchall()
        }
        if "bet_no" not in columns:
            connection.execute("ALTER TABLE option_strategies ADD COLUMN bet_no INTEGER")
            connection.execute("UPDATE option_strategies SET bet_no = strategy_no WHERE bet_no IS NULL")
        apply_option_bet_no_migration(connection)


def apply_option_bet_no_migration(connection):
    migration_name = "options_bet_no_per_strategy_v2"
    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (migration_name,),
    ).fetchone()
    if applied:
        return

    strategy_types = connection.execute(
        """
        SELECT DISTINCT strategy_type
        FROM option_strategies
        WHERE strategy_type IS NOT NULL
        ORDER BY strategy_type
        """
    ).fetchall()
    for strategy in strategy_types:
        rows = connection.execute(
            """
            SELECT id
            FROM option_strategies
            WHERE strategy_type = ?
            ORDER BY buy_date, created_at, id
            """,
            (strategy["strategy_type"],),
        ).fetchall()
        for bet_no, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE option_strategies SET bet_no = ? WHERE id = ?",
                (bet_no, row["id"]),
            )

    connection.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (migration_name, now_iso()),
    )


def next_strategy_no():
    with connect() as connection:
        value = connection.execute(
            "SELECT COALESCE(MAX(strategy_no), 0) + 1 FROM option_strategies"
        ).fetchone()[0]
        return int(value)


def next_option_bet_no(strategy_type):
    with connect() as connection:
        value = connection.execute(
            """
            SELECT COALESCE(MAX(bet_no), 0) + 1
            FROM option_strategies
            WHERE strategy_type = ?
            """,
            (strategy_type,),
        ).fetchone()[0]
        return int(value)


def next_option_bet_numbers(strategy_types):
    return {strategy_type: next_option_bet_no(strategy_type) for strategy_type in strategy_types}


def parse_option_ticker(ticker):
    text = (ticker or "").strip().upper()
    match = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s+(CE|PE)$", text)
    if not match:
        strike_only = re.match(r"^(\d+(?:\.\d+)?)\s+(CE|PE)$", text)
        if strike_only:
            strike, option_type = strike_only.groups()
            return {
                "underlying": "",
                "expiry_text": "",
                "strike": float(strike),
                "option_type": option_type,
            }
    if not match:
        return {
            "underlying": text.split()[0] if text else "",
            "expiry_text": "",
            "strike": None,
            "option_type": "",
        }

    prefix, strike, option_type = match.groups()
    parts = prefix.split()
    underlying = parts[0] if parts else prefix
    expiry_text = " ".join(parts[1:])
    return {
        "underlying": underlying,
        "expiry_text": expiry_text,
        "strike": float(strike),
        "option_type": option_type,
    }


def realized_leg_pl(leg):
    sell_price = leg.get("sell_price")
    if sell_price is None:
        return None
    action = (leg.get("action") or "BUY").upper()
    if action == "SELL":
        return (leg["buy_price"] - sell_price) * leg["qty"]
    return (sell_price - leg["buy_price"]) * leg["qty"]


def denominator_for_strategy(data, legs):
    debit = sum(
        max(0, (leg.get("buy_price") or 0) * (leg.get("qty") or 0))
        for leg in legs
    )
    return debit, "trade capital"


def enrich_strategy_data(data, legs):
    calculated_pl = sum(
        value for value in (realized_leg_pl(leg) for leg in legs) if value is not None
    )
    if data.get("strategy_pl") is None and any(leg.get("sell_price") is not None for leg in legs):
        data["strategy_pl"] = calculated_pl

    denominator, _ = denominator_for_strategy(data, legs)
    if data.get("pl_pct") is None and data.get("strategy_pl") is not None and denominator:
        data["pl_pct"] = data["strategy_pl"] / denominator * 100

    return data


def create_option_strategy(data, legs):
    timestamp = now_iso()
    strategy_no = data.get("strategy_no") or next_strategy_no()
    bet_no = data.get("bet_no") or next_option_bet_no(data["strategy_type"])
    data = enrich_strategy_data(dict(data), legs)

    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO option_strategies (
                strategy_no, bet_no, strategy_type, underlying, buy_date, sell_date, expiry_date,
                status, margin, max_risk, max_profit, strategy_pl, pl_pct,
                stock_move_pct, long_only_pl, notes, is_test_data, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_no,
                bet_no,
                data["strategy_type"],
                data["underlying"].strip().upper(),
                data["buy_date"],
                data.get("sell_date"),
                data.get("expiry_date"),
                data.get("status") or "Open",
                data.get("margin"),
                data.get("max_risk"),
                data.get("max_profit"),
                data.get("strategy_pl"),
                data.get("pl_pct"),
                data.get("stock_move_pct"),
                data.get("long_only_pl"),
                data.get("notes"),
                1 if data.get("is_test_data") else 0,
                timestamp,
                timestamp,
            ),
        )
        strategy_id = cursor.lastrowid

        for leg_no, leg in enumerate(legs, start=1):
            parsed = parse_option_ticker(leg["ticker"])
            trade_capital = leg["qty"] * leg["buy_price"]
            achieved_pl = realized_leg_pl(leg)
            connection.execute(
                """
                INSERT INTO option_legs (
                    strategy_id, leg_no, action, ticker, underlying, expiry_text,
                    strike, option_type, qty, buy_price, trade_capital, sell_price,
                    achieved_pl, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    leg_no,
                    leg.get("action") or "BUY",
                    leg["ticker"].strip().upper(),
                    parsed["underlying"],
                    parsed["expiry_text"],
                    parsed["strike"],
                    parsed["option_type"],
                    leg["qty"],
                    leg["buy_price"],
                    trade_capital,
                    leg.get("sell_price"),
                    achieved_pl,
                    leg.get("status") or data.get("status") or "Open",
                    timestamp,
                    timestamp,
                ),
            )

        return strategy_id


def list_option_strategies(strategy_type=None):
    with connect() as connection:
        params = []
        where_clause = ""
        if strategy_type:
            where_clause = "WHERE s.strategy_type = ?"
            params.append(strategy_type)

        strategies = connection.execute(
            f"""
            SELECT
                s.*,
                COUNT(l.id) AS leg_count,
                SUM(COALESCE(l.trade_capital, 0)) AS total_trade_capital,
                CASE
                    WHEN SUM(CASE WHEN UPPER(COALESCE(l.action, '')) = 'BUY' AND (UPPER(COALESCE(l.option_type, '')) = 'CE' OR UPPER(TRIM(COALESCE(l.ticker, ''))) LIKE '% CE') THEN 1 ELSE 0 END) > 0
                    THEN SUM(CASE WHEN UPPER(COALESCE(l.action, '')) = 'BUY' AND (UPPER(COALESCE(l.option_type, '')) = 'CE' OR UPPER(TRIM(COALESCE(l.ticker, ''))) LIKE '% CE') THEN COALESCE(l.achieved_pl, 0) ELSE 0 END)
                    ELSE NULL
                END AS long_call_pl,
                CASE
                    WHEN SUM(CASE WHEN UPPER(COALESCE(l.action, '')) = 'BUY' AND (UPPER(COALESCE(l.option_type, '')) = 'PE' OR UPPER(TRIM(COALESCE(l.ticker, ''))) LIKE '% PE') THEN 1 ELSE 0 END) > 0
                    THEN SUM(CASE WHEN UPPER(COALESCE(l.action, '')) = 'BUY' AND (UPPER(COALESCE(l.option_type, '')) = 'PE' OR UPPER(TRIM(COALESCE(l.ticker, ''))) LIKE '% PE') THEN COALESCE(l.achieved_pl, 0) ELSE 0 END)
                    ELSE NULL
                END AS long_put_pl,
                CASE
                    WHEN s.stock_move_pct IS NOT NULL
                    THEN 1000000 * s.stock_move_pct / 100
                    ELSE NULL
                END AS long_stock_pl
            FROM option_strategies s
            LEFT JOIN option_legs l ON l.strategy_id = s.id
            {where_clause}
            GROUP BY s.id
            ORDER BY
                CASE WHEN LOWER(COALESCE(s.status, '')) = 'open' THEN 0 ELSE 1 END,
                s.buy_date DESC,
                s.id DESC
            """,
            params,
        ).fetchall()

        rows = []
        for strategy in strategies:
            row = dict(strategy)
            row["legs"] = [
                dict(leg)
                for leg in connection.execute(
                    """
                    SELECT *
                    FROM option_legs
                    WHERE strategy_id = ?
                    ORDER BY leg_no, id
                    """,
                    (strategy["id"],),
                ).fetchall()
            ]
            rows.append(row)
        return rows


def list_option_strategies_missing_stock_move():
    with connect() as connection:
        return connection.execute(
            """
            SELECT id, underlying, buy_date, sell_date
            FROM option_strategies
            WHERE sell_date IS NOT NULL
              AND stock_move_pct IS NULL
            ORDER BY buy_date DESC, id DESC
            """
        ).fetchall()


def update_option_stock_move(strategy_id, stock_move_pct):
    with connect() as connection:
        connection.execute(
            """
            UPDATE option_strategies
            SET stock_move_pct = ?, updated_at = ?
            WHERE id = ?
            """,
            (stock_move_pct, now_iso(), strategy_id),
        )


def get_option_strategy(strategy_id):
    with connect() as connection:
        strategy = connection.execute(
            "SELECT * FROM option_strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        legs = connection.execute(
            "SELECT * FROM option_legs WHERE strategy_id = ? ORDER BY leg_no, id",
            (strategy_id,),
        ).fetchall()
        return strategy, legs


def update_option_strategy(strategy_id, data, legs):
    timestamp = now_iso()
    data = enrich_strategy_data(dict(data), legs)

    with connect() as connection:
        connection.execute(
            """
            UPDATE option_strategies
            SET
                strategy_no = ?,
                bet_no = ?,
                strategy_type = ?,
                underlying = ?,
                buy_date = ?,
                sell_date = ?,
                expiry_date = ?,
                status = ?,
                margin = ?,
                max_risk = ?,
                max_profit = ?,
                strategy_pl = ?,
                pl_pct = ?,
                stock_move_pct = ?,
                long_only_pl = ?,
                notes = ?,
                is_test_data = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                data.get("strategy_no"),
                data.get("bet_no"),
                data["strategy_type"],
                data["underlying"].strip().upper(),
                data["buy_date"],
                data.get("sell_date"),
                data.get("expiry_date"),
                data.get("status") or "Open",
                data.get("margin"),
                data.get("max_risk"),
                data.get("max_profit"),
                data.get("strategy_pl"),
                data.get("pl_pct"),
                data.get("stock_move_pct"),
                data.get("long_only_pl"),
                data.get("notes"),
                1 if data.get("is_test_data") else 0,
                timestamp,
                strategy_id,
            ),
        )
        connection.execute("DELETE FROM option_legs WHERE strategy_id = ?", (strategy_id,))

        for leg_no, leg in enumerate(legs, start=1):
            parsed = parse_option_ticker(leg["ticker"])
            trade_capital = leg["qty"] * leg["buy_price"]
            achieved_pl = realized_leg_pl(leg)
            connection.execute(
                """
                INSERT INTO option_legs (
                    strategy_id, leg_no, action, ticker, underlying, expiry_text,
                    strike, option_type, qty, buy_price, trade_capital, sell_price,
                    achieved_pl, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    leg_no,
                    leg.get("action") or "BUY",
                    leg["ticker"].strip().upper(),
                    parsed["underlying"],
                    parsed["expiry_text"],
                    parsed["strike"],
                    parsed["option_type"],
                    leg["qty"],
                    leg["buy_price"],
                    trade_capital,
                    leg.get("sell_price"),
                    achieved_pl,
                    leg.get("status") or data.get("status") or "Open",
                    timestamp,
                    timestamp,
                ),
            )


def delete_option_strategy(strategy_id):
    with connect() as connection:
        connection.execute("DELETE FROM option_strategies WHERE id = ?", (strategy_id,))
