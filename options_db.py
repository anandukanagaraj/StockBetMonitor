import re
import sqlite3
from datetime import datetime
from pathlib import Path
from user_context import get_current_user_id


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock_monitor.db"


OPTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS option_trade_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    group_type TEXT NOT NULL DEFAULT 'LIVE',
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA temp_store = MEMORY")
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
        apply_trade_group_type_migration(connection)
        apply_trade_group_migration(connection)
        apply_user_ownership_migration(connection)
        apply_performance_indexes(connection)


def owner_filter(alias=""):
    user_id = get_current_user_id()
    return (f"{alias}user_id = ?", [user_id]) if user_id is not None else (None, [])


def apply_user_ownership_migration(connection):
    timestamp = now_iso()
    username = __import__("os").getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    admin = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not admin:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(option_trade_groups)").fetchall()}
    if "user_id" not in columns:
        connection.execute("ALTER TABLE option_trade_groups ADD COLUMN user_id INTEGER")
    connection.execute("UPDATE option_trade_groups SET user_id = ? WHERE user_id IS NULL", (admin["id"],))
    connection.execute("CREATE INDEX IF NOT EXISTS ix_option_groups_user ON option_trade_groups(user_id)")


def apply_performance_indexes(connection):
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_option_strategies_group_status_date
            ON option_strategies(trade_group_id, status COLLATE NOCASE, buy_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_option_strategies_group_strategy_date
            ON option_strategies(trade_group_id, strategy_type COLLATE NOCASE, buy_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_option_strategies_sell_date
            ON option_strategies(sell_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_option_strategies_stock_move_missing
            ON option_strategies(stock_move_pct, sell_date, trade_group_id);
        CREATE INDEX IF NOT EXISTS ix_option_strategies_underlying
            ON option_strategies(underlying COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_option_legs_strategy_leg
            ON option_legs(strategy_id, leg_no, id);
        CREATE INDEX IF NOT EXISTS ix_option_legs_strategy_action_type
            ON option_legs(strategy_id, action COLLATE NOCASE, option_type COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_option_trade_groups_active_type
            ON option_trade_groups(is_active, group_type);
        CREATE INDEX IF NOT EXISTS ix_option_trade_groups_code
            ON option_trade_groups(code COLLATE NOCASE);
        """
    )


def apply_trade_group_type_migration(connection):
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(option_trade_groups)").fetchall()
    }
    if "group_type" not in columns:
        connection.execute("ALTER TABLE option_trade_groups ADD COLUMN group_type TEXT NOT NULL DEFAULT 'LIVE'")
    connection.execute(
        """
        UPDATE option_trade_groups
        SET group_type = 'LIVE'
        WHERE group_type IS NULL OR TRIM(group_type) = ''
        """
    )


def apply_trade_group_migration(connection):
    migration_name = "options_trade_groups_v1"
    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (migration_name,),
    ).fetchone()
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(option_strategies)").fetchall()
    }
    if "trade_group_id" not in columns:
        connection.execute("ALTER TABLE option_strategies ADD COLUMN trade_group_id INTEGER")

    if applied:
        return

    timestamp = now_iso()
    connection.execute(
        """
        INSERT OR IGNORE INTO option_trade_groups
            (name, code, description, group_type, display_order, is_active, created_at, updated_at)
        VALUES ('Legacy', 'LEGACY', 'Existing option bets migrated from the original tracker.', 'LIVE', 0, 1, ?, ?)
        """,
        (timestamp, timestamp),
    )
    legacy_id = connection.execute(
        "SELECT id FROM option_trade_groups WHERE code = 'LEGACY'"
    ).fetchone()["id"]
    connection.execute(
        "UPDATE option_strategies SET trade_group_id = ? WHERE trade_group_id IS NULL",
        (legacy_id,),
    )
    rows = connection.execute(
        """
        SELECT id
        FROM option_strategies
        WHERE trade_group_id = ?
        ORDER BY buy_date, created_at, id
        """,
        (legacy_id,),
    ).fetchall()
    for bet_no, row in enumerate(rows, start=1):
        connection.execute(
            "UPDATE option_strategies SET bet_no = ? WHERE id = ?",
            (bet_no, row["id"]),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_option_group_bet_no ON option_strategies(trade_group_id, bet_no)"
    )
    connection.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (migration_name, timestamp),
    )


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


def list_trade_groups(active_only=False, reportable_only=False):
    with connect() as connection:
        filters = []
        condition, params = owner_filter("g.")
        if condition:
            filters.append(condition)
        if active_only:
            filters.append("g.is_active = 1")
        if reportable_only:
            filters.append("UPPER(COALESCE(g.group_type, 'LIVE')) = 'LIVE'")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT g.*, COUNT(s.id) AS bet_count
                FROM option_trade_groups g
                LEFT JOIN option_strategies s ON s.trade_group_id = g.id
                {where}
                GROUP BY g.id
                ORDER BY g.is_active DESC, g.display_order, bet_count DESC, g.name
                """, params
            ).fetchall()
        ]


def get_trade_group(group_id):
    with connect() as connection:
        condition, params = owner_filter()
        row = connection.execute(
            "SELECT * FROM option_trade_groups WHERE id = ?" + (" AND " + condition if condition else ""),
            [group_id, *params],
        ).fetchone()
        return dict(row) if row else None


def create_trade_group(name, code, description=None, display_order=0, group_type="LIVE"):
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO option_trade_groups
                (user_id, name, code, description, group_type, display_order, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                get_current_user_id(), name.strip(),
                code.strip().upper(),
                description or None,
                (group_type or "LIVE").strip().upper(),
                display_order,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid


def update_trade_group(group_id, name, code, description=None, display_order=0, group_type="LIVE"):
    with connect() as connection:
        connection.execute(
            """
            UPDATE option_trade_groups
            SET name = ?, code = ?, description = ?, group_type = ?, display_order = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip(),
                code.strip().upper(),
                description or None,
                (group_type or "LIVE").strip().upper(),
                display_order,
                now_iso(),
                group_id,
            ),
        )


def set_trade_group_active(group_id, is_active):
    with connect() as connection:
        connection.execute(
            "UPDATE option_trade_groups SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, now_iso(), group_id),
        )


def delete_trade_group(group_id):
    with connect() as connection:
        strategy_count = connection.execute(
            "SELECT COUNT(*) FROM option_strategies WHERE trade_group_id = ?",
            (group_id,),
        ).fetchone()[0]
        connection.execute(
            """
            DELETE FROM option_legs
            WHERE strategy_id IN (
                SELECT id FROM option_strategies WHERE trade_group_id = ?
            )
            """,
            (group_id,),
        )
        connection.execute(
            "DELETE FROM option_strategies WHERE trade_group_id = ?",
            (group_id,),
        )
        cursor = connection.execute(
            "DELETE FROM option_trade_groups WHERE id = ?",
            (group_id,),
        )
        return {"deleted": cursor.rowcount > 0, "strategy_count": strategy_count}


def next_option_bet_no(trade_group_id):
    with connect() as connection:
        value = connection.execute(
            """
            SELECT COALESCE(MAX(bet_no), 0) + 1
            FROM option_strategies
            WHERE trade_group_id = ?
            """,
            (trade_group_id,),
        ).fetchone()[0]
        return int(value)


def next_option_bet_numbers(trade_group_ids):
    return {group_id: next_option_bet_no(group_id) for group_id in trade_group_ids}


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
    if data.get("margin"):
        return data["margin"], "margin"
    if data.get("max_risk"):
        return data["max_risk"], "max risk"

    debit = sum(max(0, leg_net_cashflow(leg)) for leg in legs)
    return debit, "trade capital"


def leg_net_cashflow(leg):
    qty = leg.get("qty") or 0
    buy_price = leg.get("buy_price") or 0
    action = (leg.get("action") or "BUY").upper()
    value = buy_price * qty
    return -value if action == "SELL" else value


def option_type(leg):
    parsed = parse_option_ticker(leg.get("ticker"))
    return (leg.get("option_type") or parsed.get("option_type") or "").upper()


def leg_strike(leg):
    if leg.get("strike") is not None:
        return leg["strike"]
    return parse_option_ticker(leg.get("ticker")).get("strike")


def calculated_max_risk(data, legs):
    """Max risk for a strategy. Tickers are parsed here (this module has the
    fuller option-ticker parser); the maths lives in services.option_charges."""
    from services.option_charges import calculated_option_max_risk

    parsed_legs = [{**dict(leg), "strike": leg_strike(leg), "option_type": option_type(leg)} for leg in legs]
    return calculated_option_max_risk({"strategy_type": data.get("strategy_type"), "legs": parsed_legs})


def enrich_strategy_data(data, legs):
    calculated_pl = sum(
        value for value in (realized_leg_pl(leg) for leg in legs) if value is not None
    )
    if data.get("strategy_pl") is None and any(leg.get("sell_price") is not None for leg in legs):
        data["strategy_pl"] = calculated_pl

    if data.get("max_risk") is None:
        data["max_risk"] = calculated_max_risk(data, legs)

    denominator, _ = denominator_for_strategy(data, legs)
    if data.get("pl_pct") is None and data.get("strategy_pl") is not None and denominator:
        data["pl_pct"] = data["strategy_pl"] / denominator * 100

    return data


def create_option_strategy(data, legs):
    timestamp = now_iso()
    strategy_no = data.get("strategy_no") or next_strategy_no()
    data = enrich_strategy_data(dict(data), legs)

    with connect() as connection:
        bet_no = connection.execute(
            "SELECT COALESCE(MAX(bet_no), 0) + 1 FROM option_strategies WHERE trade_group_id = ?",
            (data["trade_group_id"],),
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO option_strategies (
                strategy_no, bet_no, trade_group_id, strategy_type, underlying, buy_date, sell_date, expiry_date,
                status, margin, max_risk, max_profit, strategy_pl, pl_pct,
                stock_move_pct, long_only_pl, notes, is_test_data, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_no,
                bet_no,
                data["trade_group_id"],
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
                0,
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


def list_option_strategies(strategy_type=None, trade_group_id=None, reportable_only=True):
    with connect() as connection:
        params = []
        filters = []
        condition, owner_params = owner_filter("g.")
        if condition:
            filters.append(condition)
            params.extend(owner_params)
        if reportable_only:
            filters.append("g.is_active = 1")
            filters.append("UPPER(COALESCE(g.group_type, 'LIVE')) = 'LIVE'")
        if strategy_type:
            filters.append("s.strategy_type = ?")
            params.append(strategy_type)
        if trade_group_id:
            filters.append("s.trade_group_id = ?")
            params.append(trade_group_id)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        strategies = connection.execute(
            f"""
            SELECT
                s.*,
                g.name AS trade_group_name,
                g.code AS trade_group_code,
                g.group_type AS trade_group_type,
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
                    THEN
                        CASE
                            WHEN LOWER(COALESCE(s.strategy_type, '')) IN ('bear put spread', 'long put', 'bear call spread')
                            THEN -1000000 * s.stock_move_pct / 100
                            ELSE 1000000 * s.stock_move_pct / 100
                        END
                    ELSE NULL
                END AS long_stock_pl
            FROM option_strategies s
            JOIN option_trade_groups g ON g.id = s.trade_group_id
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
        strategy_rows = [dict(strategy) for strategy in strategies]
        if strategy_rows:
            placeholders = ",".join("?" for _ in strategy_rows)
            strategy_ids = [row["id"] for row in strategy_rows]
            legs_by_strategy = {row["id"]: [] for row in strategy_rows}
            for leg in connection.execute(
                f"""
                SELECT *
                FROM option_legs
                WHERE strategy_id IN ({placeholders})
                ORDER BY strategy_id, leg_no, id
                """,
                strategy_ids,
            ).fetchall():
                legs_by_strategy[leg["strategy_id"]].append(dict(leg))
            for row in strategy_rows:
                row["legs"] = legs_by_strategy.get(row["id"], [])
                rows.append(row)
        return rows


def list_option_strategies_missing_stock_move(limit=None):
    with connect() as connection:
        params = []
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(int(limit))
        return connection.execute(
            f"""
            SELECT s.id, s.underlying, s.buy_date, s.sell_date
            FROM option_strategies s
            JOIN option_trade_groups g ON g.id = s.trade_group_id
            WHERE s.sell_date IS NOT NULL
              AND s.stock_move_pct IS NULL
              AND g.is_active = 1
              AND UPPER(COALESCE(g.group_type, 'LIVE')) = 'LIVE'
            ORDER BY s.buy_date DESC, s.id DESC
            {limit_clause}
            """,
            params,
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
        condition, params = owner_filter("g.")
        strategy = connection.execute(
            f"""
            SELECT s.*, g.name AS trade_group_name, g.code AS trade_group_code, g.group_type AS trade_group_type
            FROM option_strategies s
            JOIN option_trade_groups g ON g.id = s.trade_group_id
            WHERE s.id = ? {"AND " + condition if condition else ""}
            """,
            [strategy_id, *params],
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
        current = connection.execute(
            "SELECT trade_group_id, bet_no FROM option_strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if current and current["trade_group_id"] == data["trade_group_id"]:
            bet_no = current["bet_no"]
        else:
            bet_no = connection.execute(
                "SELECT COALESCE(MAX(bet_no), 0) + 1 FROM option_strategies WHERE trade_group_id = ?",
                (data["trade_group_id"],),
            ).fetchone()[0]
        connection.execute(
            """
            UPDATE option_strategies
            SET
                strategy_no = ?,
                bet_no = ?,
                trade_group_id = ?,
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
                bet_no,
                data["trade_group_id"],
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
                0,
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
