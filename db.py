import sqlite3
from datetime import datetime
from pathlib import Path
from user_context import get_current_user_id


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "stock_monitor.db"
DEFAULT_STOCK_GROUP_CODE = "SWING"
DEFAULT_STOCK_GROUP_NAME = "Swing Trading"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    email TEXT,
    whatsapp_number TEXT,
    telegram_chat_id TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_trade_groups (
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

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_no INTEGER,
    trade_group_id INTEGER,
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
    updated_at TEXT NOT NULL,
    FOREIGN KEY(trade_group_id) REFERENCES stock_trade_groups(id),
    UNIQUE(trade_group_id, bet_no)
);

CREATE TABLE IF NOT EXISTS stock_booking_simulation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL UNIQUE,
    total_bets INTEGER NOT NULL,
    total_capital REAL NOT NULL,
    realized_profit REAL NOT NULL,
    realized_count INTEGER NOT NULL,
    winning_realized_count INTEGER NOT NULL,
    actual_win_rate REAL,
    actual_winning_capital_rate REAL,
    recommended_rate TEXT,
    recommended_score REAL,
    highest_profit_rate TEXT,
    highest_profit REAL,
    confidence TEXT,
    missing_history INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_booking_simulation_snapshot_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    profit_rate REAL NOT NULL,
    label TEXT NOT NULL,
    booked_profit REAL NOT NULL,
    hit_count INTEGER NOT NULL,
    hit_rate REAL,
    avg_days_to_hit REAL,
    capital_hit_rate REAL,
    profit_per_analysed_bet REAL,
    decision_score REAL,
    rank_no INTEGER,
    is_recommended INTEGER NOT NULL DEFAULT 0,
    is_highest_profit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES stock_booking_simulation_snapshots(id) ON DELETE CASCADE,
    UNIQUE(snapshot_id, profit_rate)
);

CREATE TABLE IF NOT EXISTS stock_stop_recovery_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    position_id INTEGER NOT NULL,
    stop_rate REAL NOT NULL,
    stop_price REAL NOT NULL,
    stop_hit INTEGER NOT NULL DEFAULT 0,
    stop_date TEXT,
    simulated_exit_price REAL,
    recovered INTEGER NOT NULL DEFAULT 0,
    recovery_date TEXT,
    recovery_days INTEGER,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE,
    UNIQUE(user_id, position_id, stop_rate)
);

CREATE TABLE IF NOT EXISTS stock_price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    direction TEXT NOT NULL DEFAULT 'ABOVE',
    channel TEXT NOT NULL DEFAULT 'BOTH',
    notes TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_price REAL,
    last_checked_at TEXT,
    triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_performance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL UNIQUE,
    total_pl REAL NOT NULL,
    realized_pl REAL NOT NULL,
    unrealized_pl REAL NOT NULL,
    created_at TEXT NOT NULL
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


def init_db():
    with connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS position_price_review_holds (
                position_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL,
                source_url TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE
            )
        """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS block_held_position_quote
            BEFORE UPDATE OF current_price ON positions
            WHEN NEW.current_price IS NOT NULL AND EXISTS (
                SELECT 1 FROM position_price_review_holds WHERE position_id = NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Price refresh paused pending corporate action holdings review');
            END
        """)
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
            for row in connection.execute("PRAGMA table_info(positions)").fetchall()
        }
        if "is_test_data" not in columns:
            connection.execute("ALTER TABLE positions ADD COLUMN is_test_data INTEGER NOT NULL DEFAULT 0")
        if "trade_group_id" not in columns:
            connection.execute("ALTER TABLE positions ADD COLUMN trade_group_id INTEGER")
        apply_stock_trade_group_migration(connection)
        apply_stock_bet_no_group_migration(connection)
        apply_stock_bet_no_per_group_sequence_migration(connection)
        apply_stock_booking_simulation_snapshot_migration(connection)
        apply_stock_price_alert_migration(connection)
        apply_portfolio_performance_snapshot_migration(connection)
        apply_user_ownership_migration(connection)
        apply_performance_indexes(connection)


def apply_user_ownership_migration(connection):
    """Assign all pre-existing data to the configured bootstrap admin."""
    timestamp = now_iso()
    username = __import__("os").getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    password_hash = __import__("os").getenv("ADMIN_PASSWORD_HASH") or ""
    connection.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, is_admin, is_active, created_at, updated_at) VALUES (?, ?, 1, 1, ?, ?)",
        (username, password_hash, timestamp, timestamp),
    )
    admin_id = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
    for column in ("full_name", "email", "whatsapp_number", "telegram_chat_id"):
        if column not in user_columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    for table in ("stock_trade_groups", "stock_price_alerts", "portfolio_performance_snapshots", "stock_booking_simulation_snapshots"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if "user_id" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
        connection.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (admin_id,))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_stock_group_user_code ON stock_trade_groups(user_id, code COLLATE NOCASE)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_stock_groups_user ON stock_trade_groups(user_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_stock_alerts_user ON stock_price_alerts(user_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_user_at ON portfolio_performance_snapshots(user_id, snapshot_at DESC)")


def current_owner_id():
    return get_current_user_id()


def owner_filter(alias=""):
    user_id = current_owner_id()
    return (f"{alias}user_id = ?", [user_id]) if user_id is not None else (None, [])


def get_user_by_username(username):
    with connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def list_users():
    with connect() as connection:
        return [dict(row) for row in connection.execute("SELECT id, username, full_name, email, whatsapp_number, telegram_chat_id, is_admin, is_active, created_at FROM users ORDER BY username").fetchall()]


def create_user(username, password_hash, full_name=None, email=None, whatsapp_number=None, telegram_chat_id=None, is_admin=False):
    timestamp = now_iso()
    with connect() as connection:
        return connection.execute(
            "INSERT INTO users (username, password_hash, full_name, email, whatsapp_number, telegram_chat_id, is_admin, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (username.strip(), password_hash, full_name, email, whatsapp_number, telegram_chat_id, 1 if is_admin else 0, timestamp, timestamp),
        ).lastrowid


def set_user_active(user_id, is_active):
    with connect() as connection:
        connection.execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?", (1 if is_active else 0, now_iso(), user_id))


def set_user_password(user_id, password_hash):
    with connect() as connection:
        connection.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, now_iso(), user_id))


def update_user_profile(user_id, full_name, email, whatsapp_number, telegram_chat_id):
    with connect() as connection:
        connection.execute(
            "UPDATE users SET full_name = ?, email = ?, whatsapp_number = ?, telegram_chat_id = ?, updated_at = ? WHERE id = ?",
            (full_name or None, email or None, whatsapp_number or None, telegram_chat_id or None, now_iso(), user_id),
        )


def apply_performance_indexes(connection):
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_positions_trade_group_open
            ON positions(trade_group_id, sell_price, sell_date, status);
        CREATE INDEX IF NOT EXISTS ix_positions_trade_group_buy_date
            ON positions(trade_group_id, buy_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_positions_trade_group_sell_date
            ON positions(trade_group_id, sell_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_positions_created_at
            ON positions(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_positions_updated_at
            ON positions(updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_positions_ticker
            ON positions(ticker COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_positions_status
            ON positions(status COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_stock_trade_groups_code
            ON stock_trade_groups(code COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_stock_trade_groups_active_type
            ON stock_trade_groups(is_active, group_type);
        CREATE INDEX IF NOT EXISTS ix_stock_booking_snapshots_date
            ON stock_booking_simulation_snapshots(snapshot_date DESC);
        CREATE INDEX IF NOT EXISTS ix_stock_booking_snapshot_rates_snapshot
            ON stock_booking_simulation_snapshot_rates(snapshot_id, rank_no);
        CREATE INDEX IF NOT EXISTS ix_stock_price_alerts_active
            ON stock_price_alerts(is_active, triggered_at, ticker COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_stock_price_alerts_ticker
            ON stock_price_alerts(ticker COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_portfolio_performance_snapshots_at
            ON portfolio_performance_snapshots(snapshot_at DESC);
        """
    )


def apply_portfolio_performance_snapshot_migration(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL UNIQUE,
            total_pl REAL NOT NULL,
            realized_pl REAL NOT NULL,
            unrealized_pl REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def apply_stock_booking_simulation_snapshot_migration(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_booking_simulation_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL UNIQUE,
            total_bets INTEGER NOT NULL,
            total_capital REAL NOT NULL,
            realized_profit REAL NOT NULL,
            realized_count INTEGER NOT NULL,
            winning_realized_count INTEGER NOT NULL,
            actual_win_rate REAL,
            actual_winning_capital_rate REAL,
            recommended_rate TEXT,
            recommended_score REAL,
            highest_profit_rate TEXT,
            highest_profit REAL,
            confidence TEXT,
            missing_history INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_booking_simulation_snapshot_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            profit_rate REAL NOT NULL,
            label TEXT NOT NULL,
            booked_profit REAL NOT NULL,
            hit_count INTEGER NOT NULL,
            hit_rate REAL,
            avg_days_to_hit REAL,
            capital_hit_rate REAL,
            profit_per_analysed_bet REAL,
            decision_score REAL,
            rank_no INTEGER,
            is_recommended INTEGER NOT NULL DEFAULT 0,
            is_highest_profit INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES stock_booking_simulation_snapshots(id) ON DELETE CASCADE,
            UNIQUE(snapshot_id, profit_rate)
        );
        """
    )


def apply_stock_price_alert_migration(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            trigger_price REAL NOT NULL,
            direction TEXT NOT NULL DEFAULT 'ABOVE',
            channel TEXT NOT NULL DEFAULT 'BOTH',
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_price REAL,
            last_checked_at TEXT,
            triggered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def apply_stock_trade_group_migration(connection):
    group_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(stock_trade_groups)").fetchall()
    }
    if "group_type" not in group_columns:
        connection.execute("ALTER TABLE stock_trade_groups ADD COLUMN group_type TEXT NOT NULL DEFAULT 'LIVE'")

    timestamp = now_iso()
    connection.execute(
        """
        INSERT OR IGNORE INTO stock_trade_groups
            (name, code, description, group_type, display_order, is_active, created_at, updated_at)
        VALUES (?, ?, ?, 'LIVE', 0, 1, ?, ?)
        """,
        (
            DEFAULT_STOCK_GROUP_NAME,
            DEFAULT_STOCK_GROUP_CODE,
            "Default group for existing swing-trading stock bets.",
            timestamp,
            timestamp,
        ),
    )
    default_group_id = connection.execute(
        "SELECT id FROM stock_trade_groups WHERE code = ?",
        (DEFAULT_STOCK_GROUP_CODE,),
    ).fetchone()["id"]
    connection.execute(
        "UPDATE positions SET trade_group_id = ? WHERE trade_group_id IS NULL",
        (default_group_id,),
    )
    connection.execute(
        """
        UPDATE stock_trade_groups
        SET group_type = 'LIVE'
        WHERE group_type IS NULL OR TRIM(group_type) = ''
        """
    )


def apply_stock_bet_no_group_migration(connection):
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'positions'"
    ).fetchone()["sql"]
    has_global_unique_bet_no = "bet_no INTEGER UNIQUE" in table_sql

    if has_global_unique_bet_no:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE positions RENAME TO positions_old")
        connection.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_no INTEGER,
                trade_group_id INTEGER,
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
                updated_at TEXT NOT NULL,
                FOREIGN KEY(trade_group_id) REFERENCES stock_trade_groups(id),
                UNIQUE(trade_group_id, bet_no)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO positions (
                id, bet_no, trade_group_id, ticker, buy_date, qty, buy_price,
                target_price, stop_loss, current_price, sell_date, sell_price,
                status, notes, is_test_data, created_at, updated_at
            )
            SELECT
                id, bet_no, trade_group_id, ticker, buy_date, qty, buy_price,
                target_price, stop_loss, current_price, sell_date, sell_price,
                status, notes, COALESCE(is_test_data, 0), created_at, updated_at
            FROM positions_old
            """
        )
        connection.execute("DROP TABLE positions_old")
        connection.execute("PRAGMA foreign_keys = ON")

    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_group_bet_no ON positions(trade_group_id, bet_no)"
    )


def apply_stock_bet_no_per_group_sequence_migration(connection):
    migration_name = "stock_bet_no_per_group_sequence_v1"
    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (migration_name,),
    ).fetchone()
    if applied:
        return

    group_rows = connection.execute(
        "SELECT id FROM stock_trade_groups ORDER BY display_order, id"
    ).fetchall()
    for group in group_rows:
        rows = connection.execute(
            """
            SELECT id
            FROM positions
            WHERE trade_group_id = ?
            ORDER BY
                CASE WHEN bet_no IS NULL THEN 1 ELSE 0 END,
                bet_no,
                created_at,
                id
            """,
            (group["id"],),
        ).fetchall()

        for sequence, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE positions SET bet_no = ? WHERE id = ?",
                (-sequence, row["id"]),
            )
        for sequence, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE positions SET bet_no = ? WHERE id = ?",
                (sequence, row["id"]),
            )

    connection.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (migration_name, now_iso()),
    )


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def next_bet_no(trade_group_id=None):
    with connect() as connection:
        group_id = trade_group_id or default_stock_group_id(connection)
        value = connection.execute(
            "SELECT COALESCE(MAX(bet_no), 0) + 1 FROM positions WHERE trade_group_id = ?",
            (group_id,),
        ).fetchone()[0]
        return int(value)


def next_stock_bet_numbers(group_ids):
    if not group_ids:
        return {}
    placeholders = ", ".join("?" for _ in group_ids)
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT trade_group_id, COALESCE(MAX(bet_no), 0) + 1 AS next_bet_no
            FROM positions
            WHERE trade_group_id IN ({placeholders})
            GROUP BY trade_group_id
            """,
            list(group_ids),
        ).fetchall()
        values = {row["trade_group_id"]: int(row["next_bet_no"]) for row in rows}
        return {group_id: values.get(group_id, 1) for group_id in group_ids}


def default_stock_group_id(connection):
    condition, params = owner_filter()
    where = "WHERE code = ?" + (" AND " + condition if condition else "")
    row = connection.execute(
        f"SELECT id FROM stock_trade_groups {where}",
        [DEFAULT_STOCK_GROUP_CODE, *params],
    ).fetchone()
    return row["id"] if row else None


def list_stock_trade_groups(active_only=False, reportable_only=False):
    with connect() as connection:
        filters = []
        condition, owner_params = owner_filter("g.")
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
                SELECT g.*, COUNT(p.id) AS bet_count
                FROM stock_trade_groups g
                LEFT JOIN positions p ON p.trade_group_id = g.id
                {where}
                GROUP BY g.id
                ORDER BY g.is_active DESC, g.display_order, bet_count DESC, g.name
                """, owner_params
            ).fetchall()
        ]


def get_stock_trade_group(group_id):
    with connect() as connection:
        condition, params = owner_filter()
        row = connection.execute(
            "SELECT * FROM stock_trade_groups WHERE id = ?" + (" AND " + condition if condition else ""),
            [group_id, *params],
        ).fetchone()
        return dict(row) if row else None


def get_default_stock_trade_group():
    with connect() as connection:
        condition, params = owner_filter()
        row = connection.execute(
            "SELECT * FROM stock_trade_groups WHERE code = ?" + (" AND " + condition if condition else ""),
            [DEFAULT_STOCK_GROUP_CODE, *params],
        ).fetchone()
        return dict(row) if row else None


def create_stock_trade_group(name, code, description=None, display_order=0, group_type="LIVE"):
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO stock_trade_groups
                (user_id, name, code, description, group_type, display_order, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                current_owner_id(), name.strip(),
                code.strip().upper(),
                description or None,
                (group_type or "LIVE").strip().upper(),
                display_order,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid


def update_stock_trade_group(group_id, name, code, description=None, display_order=0, group_type="LIVE"):
    with connect() as connection:
        connection.execute(
            """
            UPDATE stock_trade_groups
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


def set_stock_trade_group_active(group_id, is_active):
    with connect() as connection:
        connection.execute(
            "UPDATE stock_trade_groups SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, now_iso(), group_id),
        )


def delete_paper_stock_trade_group(group_id):
    """Permanently delete an owned PAPER group and every position in it."""
    with connect() as connection:
        condition, params = owner_filter()
        group = connection.execute(
            "SELECT * FROM stock_trade_groups WHERE id = ?" + (" AND " + condition if condition else ""),
            [group_id, *params],
        ).fetchone()
        if not group:
            raise ValueError("Stock Trade Group not found.")
        if (group["group_type"] or "LIVE").upper() != "PAPER":
            raise ValueError("Only Paper Trade groups can be permanently deleted.")
        position_count = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE trade_group_id = ?", (group_id,)
        ).fetchone()[0]
        existing_tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        for baseline_table in ("fund_position_baselines", "fund_stock_baseline"):
            if baseline_table in existing_tables:
                connection.execute(
                    f"DELETE FROM {baseline_table} WHERE position_id IN (SELECT id FROM positions WHERE trade_group_id = ?)",
                    (group_id,),
                )
        connection.execute("DELETE FROM positions WHERE trade_group_id = ?", (group_id,))
        connection.execute("DELETE FROM stock_trade_groups WHERE id = ?", (group_id,))
        return {"name": group["name"], "position_count": position_count}


def stock_group_filter_clause(trade_group_id=None, trade_group_code=DEFAULT_STOCK_GROUP_CODE, reportable_only=True):
    filters = []
    params = []
    condition, owner_params = owner_filter("g.")
    if condition:
        filters.append(condition)
        params.extend(owner_params)
    if trade_group_id is not None:
        filters.append("p.trade_group_id = ?")
        params.append(trade_group_id)
    elif trade_group_code:
        filters.append("g.code = ?")
        params.append(trade_group_code)
    if reportable_only:
        filters.append("g.is_active = 1")
        filters.append("UPPER(COALESCE(g.group_type, 'LIVE')) = 'LIVE'")
    return filters, params


def list_positions(trade_group_id=None, trade_group_code=DEFAULT_STOCK_GROUP_CODE, reportable_only=True):
    with connect() as connection:
        filters, params = stock_group_filter_clause(trade_group_id, trade_group_code, reportable_only)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        return connection.execute(
            f"""
            SELECT p.*, g.name AS trade_group_name, g.code AS trade_group_code, g.group_type AS trade_group_type
            FROM positions p
            JOIN stock_trade_groups g ON g.id = p.trade_group_id
            {where_clause}
            ORDER BY
                CASE
                    WHEN p.sell_price IS NULL AND p.sell_date IS NULL THEN 0
                    ELSE 1
                END,
                p.buy_date DESC,
                p.id DESC
            """,
            params,
        ).fetchall()


def list_positions_for_management(trade_group_id=None):
    with connect() as connection:
        filters = [
            "p.sell_price IS NULL",
            "p.sell_date IS NULL",
            "LOWER(COALESCE(p.status, '')) NOT IN ('closed', 'target hit', 'stop loss hit', 'sold')",
        ]
        params = []
        condition, owner_params = owner_filter("g.")
        if condition:
            filters.append(condition)
            params.extend(owner_params)
        if trade_group_id is not None:
            filters.append("p.trade_group_id = ?")
            params.append(trade_group_id)
        where_clause = f"WHERE {' AND '.join(filters)}"
        return connection.execute(
            f"""
            SELECT p.*, g.name AS trade_group_name, g.code AS trade_group_code, g.group_type AS trade_group_type
            FROM positions p
            JOIN stock_trade_groups g ON g.id = p.trade_group_id
            {where_clause}
            ORDER BY
                COALESCE(p.created_at, p.updated_at) DESC,
                p.id DESC
            """,
            params,
        ).fetchall()


def list_test_positions():
    with connect() as connection:
        return connection.execute(
            """
            SELECT p.*, g.name AS trade_group_name, g.code AS trade_group_code, g.group_type AS trade_group_type
            FROM positions p
            LEFT JOIN stock_trade_groups g ON g.id = p.trade_group_id
            WHERE UPPER(COALESCE(g.group_type, 'LIVE')) = 'PAPER'
               OR COALESCE(p.is_test_data, 0) = 1
               OR UPPER(COALESCE(p.ticker, '')) = 'TEST'
            ORDER BY
                COALESCE(p.updated_at, p.created_at) DESC,
                p.id DESC
            """
        ).fetchall()


def record_stock_booking_simulation_snapshot(summary, snapshot_date=None):
    snapshot_date = snapshot_date or datetime.now().date().isoformat()
    timestamp = now_iso()
    analytics = summary.get("analytics") or {}
    with connect() as connection:
        existing = connection.execute(
            "SELECT id, created_at FROM stock_booking_simulation_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        ).fetchone()
        values = (
            snapshot_date,
            int(summary.get("bets") or 0),
            float(summary.get("capital") or 0),
            float(summary.get("realized_pl") or 0),
            int(summary.get("realized_count") or 0),
            int(summary.get("winning_realized_count") or 0),
            summary.get("actual_win_rate"),
            summary.get("actual_winning_capital_rate"),
            analytics.get("best_label"),
            analytics.get("best_score"),
            analytics.get("highest_profit_label"),
            analytics.get("highest_profit"),
            analytics.get("confidence"),
            int(summary.get("missing_history") or 0),
            existing["created_at"] if existing else timestamp,
            timestamp,
        )
        connection.execute(
            """
            INSERT INTO stock_booking_simulation_snapshots (
                snapshot_date, total_bets, total_capital, realized_profit, realized_count,
                winning_realized_count, actual_win_rate, actual_winning_capital_rate,
                recommended_rate, recommended_score, highest_profit_rate, highest_profit,
                confidence, missing_history, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                total_bets = excluded.total_bets,
                total_capital = excluded.total_capital,
                realized_profit = excluded.realized_profit,
                realized_count = excluded.realized_count,
                winning_realized_count = excluded.winning_realized_count,
                actual_win_rate = excluded.actual_win_rate,
                actual_winning_capital_rate = excluded.actual_winning_capital_rate,
                recommended_rate = excluded.recommended_rate,
                recommended_score = excluded.recommended_score,
                highest_profit_rate = excluded.highest_profit_rate,
                highest_profit = excluded.highest_profit,
                confidence = excluded.confidence,
                missing_history = excluded.missing_history,
                updated_at = excluded.updated_at
            """,
            values,
        )
        snapshot_id = connection.execute(
            "SELECT id FROM stock_booking_simulation_snapshots WHERE snapshot_date = ?",
            (snapshot_date,),
        ).fetchone()["id"]
        connection.execute(
            "DELETE FROM stock_booking_simulation_snapshot_rates WHERE snapshot_id = ?",
            (snapshot_id,),
        )
        ranking_by_label = {
            item["label"]: item
            for item in summary.get("scenario_rankings", [])
        }
        for rate, target in (summary.get("targets") or {}).items():
            ranking = ranking_by_label.get(target.get("label"), {})
            connection.execute(
                """
                INSERT INTO stock_booking_simulation_snapshot_rates (
                    snapshot_id, profit_rate, label, booked_profit, hit_count, hit_rate,
                    avg_days_to_hit, capital_hit_rate, profit_per_analysed_bet,
                    decision_score, rank_no, is_recommended, is_highest_profit,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    float(rate),
                    target.get("label"),
                    float(target.get("booked_profit") or 0),
                    int(target.get("hits") or 0),
                    target.get("hit_rate"),
                    target.get("avg_days_to_hit"),
                    target.get("capital_hit_rate"),
                    target.get("profit_per_analysed_bet"),
                    target.get("decision_score"),
                    ranking.get("rank"),
                    1 if target.get("is_best") else 0,
                    1 if target.get("is_profit_best") else 0,
                    timestamp,
                    timestamp,
                ),
            )
        return snapshot_id


def record_stock_stop_recovery_audit(rows):
    timestamp = now_iso()
    owner_id = get_current_user_id()
    with connect() as connection:
        for row in rows:
            for key, rate in (("stop_loss", 0.15), ("stop_75", 0.75)):
                result = row[key]
                connection.execute(
                    """INSERT INTO stock_stop_recovery_audit
                    (user_id, position_id, stop_rate, stop_price, stop_hit, stop_date,
                     simulated_exit_price, recovered, recovery_date, recovery_days, evaluated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, position_id, stop_rate) DO UPDATE SET
                    stop_price=excluded.stop_price, stop_hit=excluded.stop_hit,
                    stop_date=excluded.stop_date, simulated_exit_price=excluded.simulated_exit_price,
                    recovered=excluded.recovered, recovery_date=excluded.recovery_date,
                    recovery_days=excluded.recovery_days, evaluated_at=excluded.evaluated_at""",
                    (owner_id, row["position_id"], rate, result["stop_price"], int(result["hit"]),
                     result.get("hit_date"), result.get("exit_price"), int(result.get("recovered", False)),
                     result.get("recovery_date"), result.get("recovery_days"), timestamp),
                )


def list_stock_booking_simulation_snapshots(limit=12):
    with connect() as connection:
        condition, params = owner_filter()
        snapshots = connection.execute(
            f"""
            SELECT *
            FROM stock_booking_simulation_snapshots
            {"WHERE " + condition if condition else ""}
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        result = []
        for snapshot in snapshots:
            rates = connection.execute(
                """
                SELECT *
                FROM stock_booking_simulation_snapshot_rates
                WHERE snapshot_id = ?
                ORDER BY COALESCE(rank_no, 999), profit_rate
                """,
                (snapshot["id"],),
            ).fetchall()
            item = dict(snapshot)
            item["rates"] = [dict(row) for row in rates]
            result.append(item)
        return result


def list_stock_price_alerts(active_only=False):
    with connect() as connection:
        filters = ["is_active = 1"] if active_only else []
        condition, params = owner_filter()
        if condition:
            filters.append(condition)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT *
                FROM stock_price_alerts
                {where}
                ORDER BY
                    is_active DESC,
                    CASE WHEN triggered_at IS NULL THEN 0 ELSE 1 END,
                    COALESCE(updated_at, created_at) DESC,
                    ticker COLLATE NOCASE
                """, params
            ).fetchall()
        ]


def get_stock_price_alert(alert_id):
    with connect() as connection:
        condition, params = owner_filter()
        row = connection.execute(
            "SELECT * FROM stock_price_alerts WHERE id = ?" + (" AND " + condition if condition else ""),
            [alert_id, *params],
        ).fetchone()
        return dict(row) if row else None


def create_stock_price_alert(data):
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO stock_price_alerts (
                user_id, ticker, trigger_price, direction, channel, notes, is_active,
                last_price, last_checked_at, triggered_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                current_owner_id(), data["ticker"].strip().upper(),
                data["trigger_price"],
                data.get("direction") or "ABOVE",
                data.get("channel") or "BOTH",
                data.get("notes") or None,
                1 if data.get("is_active", True) else 0,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid


def update_stock_price_alert(alert_id, data):
    with connect() as connection:
        connection.execute(
            """
            UPDATE stock_price_alerts
            SET ticker = ?,
                trigger_price = ?,
                direction = ?,
                channel = ?,
                notes = ?,
                is_active = ?,
                triggered_at = CASE WHEN ? = 1 THEN NULL ELSE triggered_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                data["ticker"].strip().upper(),
                data["trigger_price"],
                data.get("direction") or "ABOVE",
                data.get("channel") or "BOTH",
                data.get("notes") or None,
                1 if data.get("is_active", True) else 0,
                1 if data.get("reset_triggered") else 0,
                now_iso(),
                alert_id,
            ),
        )


def delete_stock_price_alert(alert_id):
    with connect() as connection:
        connection.execute("DELETE FROM stock_price_alerts WHERE id = ?", (alert_id,))


def mark_stock_price_alert_checked(alert_id, price):
    with connect() as connection:
        connection.execute(
            """
            UPDATE stock_price_alerts
            SET last_price = ?, last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (price, now_iso(), now_iso(), alert_id),
        )


def mark_stock_price_alert_triggered(alert_id, price):
    timestamp = now_iso()
    with connect() as connection:
        connection.execute(
            """
            UPDATE stock_price_alerts
            SET last_price = ?,
                last_checked_at = ?,
                triggered_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (price, timestamp, timestamp, timestamp, alert_id),
        )


def get_position(position_id):
    with connect() as connection:
        condition, params = owner_filter("g.")
        return connection.execute(
            f"""
            SELECT p.*, g.name AS trade_group_name, g.code AS trade_group_code, g.group_type AS trade_group_type
            FROM positions p
            LEFT JOIN stock_trade_groups g ON g.id = p.trade_group_id
            WHERE p.id = ?
            {"AND " + condition if condition else ""}
            """,
            [position_id, *params],
        ).fetchone()


def delete_position(position_id):
    with connect() as connection:
        connection.execute("DELETE FROM positions WHERE id = ?", (position_id,))


def create_position(data):
    timestamp = now_iso()
    with connect() as connection:
        trade_group_id = data.get("trade_group_id") or default_stock_group_id(connection)
        bet_no = data.get("bet_no") or next_bet_no(trade_group_id)
        cursor = connection.execute(
            """
            INSERT INTO positions (
                bet_no, trade_group_id, ticker, buy_date, qty, buy_price, target_price, stop_loss,
                current_price, sell_date, sell_price, status, notes, is_test_data, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bet_no,
                trade_group_id,
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
                0,
                timestamp,
                timestamp,
            ),
        )
        return cursor.lastrowid


def update_position(position_id, data):
    with connect() as connection:
        trade_group_id = data.get("trade_group_id") or default_stock_group_id(connection)
        connection.execute(
            """
            UPDATE positions
            SET bet_no = ?,
                trade_group_id = ?,
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
                trade_group_id,
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
                0,
                now_iso(),
                position_id,
            ),
        )


def update_current_price(position_id, price):
    with connect() as connection:
        hold = connection.execute(
            "SELECT reason FROM position_price_review_holds WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        if hold:
            raise ValueError(f"Price refresh paused pending holdings review: {hold['reason']}")
        connection.execute(
            "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
            (price, now_iso(), position_id),
        )


def record_portfolio_performance_snapshot(summary, snapshot_at=None):
    snapshot_at = snapshot_at or now_iso()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO portfolio_performance_snapshots (
                user_id, snapshot_at, total_pl, realized_pl, unrealized_pl, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                current_owner_id(), snapshot_at,
                summary["total_actual_pl"],
                summary["total_realized_pl"],
                summary["total_unrealized_pl"],
                now_iso(),
            ),
        )


def list_portfolio_performance_snapshots(limit=2_000):
    with connect() as connection:
        condition, params = owner_filter()
        return connection.execute(
            f"""
            SELECT snapshot_at, total_pl, realized_pl, unrealized_pl
            FROM portfolio_performance_snapshots
            {"WHERE " + condition if condition else ""}
            ORDER BY snapshot_at ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()


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
