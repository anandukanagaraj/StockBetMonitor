"""AK MultiCap Fund: isolated unit/NAV accounting for SWING stock bets."""

from datetime import date, datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from db import connect, is_closed, list_positions
from services.date_utils import parse_date
from services.number_format import format_indian_decimal, format_indian_money
from services.yahoo_service import nifty50_history


fund_bp = Blueprint("fund_management", __name__, url_prefix="/fund-management")


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def init_fund_db():
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fund_settings (
                user_id INTEGER PRIMARY KEY,
                fund_name TEXT NOT NULL DEFAULT 'AK MultiCap Fund',
                opening_nav REAL NOT NULL DEFAULT 10,
                seed_units REAL NOT NULL DEFAULT 0,
                baseline_initialized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS fund_investors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                notes TEXT,
                is_test_data INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS fund_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                investor_id INTEGER,
                transaction_type TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                nav REAL,
                units REAL NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(investor_id) REFERENCES fund_investors(id)
            );
            CREATE INDEX IF NOT EXISTS ix_fund_investors_user ON fund_investors(user_id, is_active, name);
            CREATE INDEX IF NOT EXISTS ix_fund_transactions_user_date ON fund_transactions(user_id, transaction_date DESC, id DESC);
            CREATE INDEX IF NOT EXISTS ix_fund_transactions_investor ON fund_transactions(investor_id, transaction_date, id);
            CREATE TABLE IF NOT EXISTS investor_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                notes TEXT,
                is_test_data INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS ix_investor_profiles_user ON investor_profiles(user_id, is_active, name);
            CREATE TABLE IF NOT EXISTS fund_nav_snapshots (
                fund_id INTEGER NOT NULL,
                snapshot_date TEXT NOT NULL,
                nav REAL NOT NULL,
                aum REAL NOT NULL,
                total_units REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(fund_id, snapshot_date),
                FOREIGN KEY(fund_id) REFERENCES funds(id)
            );
            CREATE TABLE IF NOT EXISTS fund_stock_baseline (
                user_id INTEGER NOT NULL,
                position_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, position_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(position_id) REFERENCES positions(id)
            );
            CREATE TABLE IF NOT EXISTS funds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                opening_nav REAL NOT NULL DEFAULT 10,
                seed_units REAL NOT NULL DEFAULT 0,
                stock_scope TEXT NOT NULL DEFAULT 'SWING',
                baseline_initialized INTEGER NOT NULL DEFAULT 0,
                is_test_data INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS fund_position_baselines (
                fund_id INTEGER NOT NULL,
                position_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(fund_id, position_id),
                FOREIGN KEY(fund_id) REFERENCES funds(id),
                FOREIGN KEY(position_id) REFERENCES positions(id)
            );
            CREATE INDEX IF NOT EXISTS ix_funds_user ON funds(user_id, name);
            """
        )
        setting_columns = {row["name"] for row in connection.execute("PRAGMA table_info(fund_settings)").fetchall()}
        if "baseline_initialized" not in setting_columns:
            connection.execute("ALTER TABLE fund_settings ADD COLUMN baseline_initialized INTEGER NOT NULL DEFAULT 0")
        investor_columns = {row["name"] for row in connection.execute("PRAGMA table_info(fund_investors)").fetchall()}
        if "is_test_data" not in investor_columns:
            connection.execute("ALTER TABLE fund_investors ADD COLUMN is_test_data INTEGER NOT NULL DEFAULT 0")
        if "profile_id" not in investor_columns:
            connection.execute("ALTER TABLE fund_investors ADD COLUMN profile_id INTEGER")
        fund_columns = {row["name"] for row in connection.execute("PRAGMA table_info(funds)").fetchall()}
        if "is_test_data" not in fund_columns:
            connection.execute("ALTER TABLE funds ADD COLUMN is_test_data INTEGER NOT NULL DEFAULT 0")
        for table in ("fund_investors", "fund_transactions"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if "fund_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN fund_id INTEGER")
        # Migrate the original single-fund configuration into AK MultiCap Fund.
        for legacy in connection.execute("SELECT * FROM fund_settings").fetchall():
            fund = connection.execute("SELECT * FROM funds WHERE user_id = ? ORDER BY id LIMIT 1", (legacy["user_id"],)).fetchone()
            if not fund:
                cursor = connection.execute(
                    """INSERT INTO funds (user_id, name, opening_nav, seed_units, stock_scope, baseline_initialized, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'SWING', ?, ?, ?)""",
                    (legacy["user_id"], legacy["fund_name"], legacy["opening_nav"], legacy["seed_units"],
                     legacy["baseline_initialized"], legacy["created_at"], legacy["updated_at"]),
                )
                fund_id = cursor.lastrowid
            else:
                fund_id = fund["id"]
            connection.execute("UPDATE fund_investors SET fund_id = ? WHERE user_id = ? AND fund_id IS NULL", (fund_id, legacy["user_id"]))
            connection.execute("UPDATE fund_transactions SET fund_id = ? WHERE user_id = ? AND fund_id IS NULL", (fund_id, legacy["user_id"]))
            connection.execute(
                """INSERT OR IGNORE INTO fund_position_baselines (fund_id, position_id, created_at)
                   SELECT ?, position_id, created_at FROM fund_stock_baseline WHERE user_id = ?""",
                (fund_id, legacy["user_id"]),
            )
        # Promote legacy fund-specific contact records into the investor directory.
        for investor in connection.execute("SELECT * FROM fund_investors WHERE profile_id IS NULL").fetchall():
            profile_id = connection.execute(
                """INSERT INTO investor_profiles
                   (user_id, name, email, phone, notes, is_test_data, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (investor["user_id"], investor["name"], investor["email"], investor["phone"], investor["notes"],
                 investor["is_test_data"], investor["is_active"], investor["created_at"], investor["updated_at"]),
            ).lastrowid
            connection.execute("UPDATE fund_investors SET profile_id = ? WHERE id = ?", (profile_id, investor["id"]))


def _user_id():
    return session.get("user_id")


def _require_login():
    if not _user_id():
        return redirect(url_for("login", next=request.path))
    return None


def _float(value, label, minimum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    return number


def _stock_assets(fund):
    scope = fund["stock_scope"]
    positions = list_positions(trade_group_code=None if scope == "ALL" else scope, reportable_only=True)
    open_rows = [row for row in positions if not is_closed(row)]
    market_value = sum((row["current_price"] if row["current_price"] is not None else row["buy_price"]) * row["qty"] for row in open_rows)
    cost_value = sum(row["buy_price"] * row["qty"] for row in open_rows)
    return {"market_value": market_value, "cost_value": cost_value, "open_bets": len(open_rows), "positions": positions}


def _get_fund(user_id, fund_id):
    with connect() as connection:
        row = connection.execute("SELECT * FROM funds WHERE id = ? AND user_id = ?", (fund_id, user_id)).fetchone()
        return dict(row) if row else None


def _ensure_stock_baseline(fund, positions):
    if fund.get("baseline_initialized"):
        return
    timestamp = _now()
    with connect() as connection:
        for row in positions:
            if not is_closed(row):
                connection.execute(
                    "INSERT OR IGNORE INTO fund_position_baselines (fund_id, position_id, created_at) VALUES (?, ?, ?)",
                    (fund["id"], row["id"], timestamp),
                )
        connection.execute(
            "UPDATE funds SET baseline_initialized = 1, updated_at = ? WHERE id = ?",
            (timestamp, fund["id"]),
        )


def _automatic_stock_cash_flows(fund, positions):
    with connect() as connection:
        baseline_ids = {row["position_id"] for row in connection.execute(
            "SELECT position_id FROM fund_position_baselines WHERE fund_id = ?", (fund["id"],)
        ).fetchall()}
    started_at = fund["created_at"]
    purchases = 0.0
    sale_proceeds = 0.0
    purchase_count = 0
    sale_count = 0
    for row in positions:
        is_baseline = row["id"] in baseline_ids
        created_at = str(row["created_at"] or "")
        if not is_baseline and created_at > started_at:
            purchases += (row["buy_price"] or 0) * (row["qty"] or 0)
            purchase_count += 1
        if is_closed(row) and row["sell_price"] is not None:
            sell_date = str(row["sell_date"] or "")
            if is_baseline or created_at > started_at:
                # A baseline holding leaving the portfolio, or a post-inception
                # round trip, automatically returns its sale proceeds to cash.
                if is_baseline or sell_date >= started_at[:10]:
                    sale_proceeds += row["sell_price"] * (row["qty"] or 0)
                    sale_count += 1
    return {
        "purchases": purchases, "sale_proceeds": sale_proceeds,
        "net_cash": sale_proceeds - purchases,
        "purchase_count": purchase_count, "sale_count": sale_count,
    }


def _fund_snapshot(user_id, fund_id):
    fund = _get_fund(user_id, fund_id)
    if not fund:
        return None
    assets = _stock_assets(fund)
    _ensure_stock_baseline(fund, assets["positions"])
    stock_cash_flows = _automatic_stock_cash_flows(fund, assets["positions"])
    with connect() as connection:
        transactions = [dict(row) for row in connection.execute(
            """SELECT t.*, i.name AS investor_name FROM fund_transactions t
               LEFT JOIN fund_investors i ON i.id = t.investor_id
               WHERE t.user_id = ? AND t.fund_id = ? ORDER BY t.transaction_date DESC, t.id DESC""", (user_id, fund_id)
        ).fetchall()]
        investors = []
        for row in connection.execute(
            """SELECT i.*, p.name AS directory_name, p.email AS directory_email, p.phone AS directory_phone,
                      p.notes AS directory_notes, p.is_test_data AS directory_is_test_data, p.is_active AS directory_is_active
               FROM fund_investors i LEFT JOIN investor_profiles p ON p.id = i.profile_id
               WHERE i.user_id = ? AND i.fund_id = ? ORDER BY i.is_active DESC, COALESCE(p.name, i.name)""",
            (user_id, fund_id),
        ).fetchall():
            item = dict(row)
            if item["profile_id"]:
                item.update(name=item["directory_name"], email=item["directory_email"], phone=item["directory_phone"],
                            notes=item["directory_notes"], is_test_data=item["directory_is_test_data"],
                            is_active=bool(item["is_active"] and item["directory_is_active"]))
            investors.append(item)

    cash = stock_cash_flows["net_cash"]
    issued_units = 0.0
    for tx in transactions:
        kind = tx["transaction_type"]
        if kind in {"SUBSCRIPTION", "CASH_IN"}:
            cash += tx["amount"]
        elif kind in {"REDEMPTION", "CASH_OUT"}:
            cash -= tx["amount"]
        if kind == "SUBSCRIPTION":
            issued_units += tx["units"]
        elif kind == "REDEMPTION":
            issued_units -= tx["units"]

    total_units = fund["seed_units"] + issued_units
    aum = assets["market_value"] + cash
    nav = aum / total_units if total_units > 0 else fund["opening_nav"]
    investor_rows = []
    allocated_units = 0.0
    for investor in investors:
        own = [tx for tx in transactions if tx["investor_id"] == investor["id"]]
        units = sum(tx["units"] if tx["transaction_type"] in {"ALLOCATION", "SUBSCRIPTION"} else -tx["units"] if tx["transaction_type"] == "REDEMPTION" else 0 for tx in own)
        subscribed = sum(tx["amount"] for tx in own if tx["transaction_type"] in {"ALLOCATION", "SUBSCRIPTION"})
        redeemed = sum(tx["amount"] for tx in own if tx["transaction_type"] == "REDEMPTION")
        value = units * nav
        allocated_units += units
        investor_rows.append({**investor, "units": units, "ownership_pct": units / total_units * 100 if total_units else 0,
                              "current_value": value, "subscribed": subscribed, "redeemed": redeemed,
                              "gain": value + redeemed - subscribed})

    result = {
        "settings": {**fund, "fund_name": fund["name"]}, "fund": fund, "assets": assets, "transactions": transactions, "investors": investor_rows,
        "stock_cash_flows": stock_cash_flows,
        "cash": cash, "aum": aum, "nav": nav, "total_units": total_units,
        "allocated_units": allocated_units, "unallocated_units": max(0.0, total_units - allocated_units),
        "allocated_pct": allocated_units / total_units * 100 if total_units else 0,
        "liquidity_shortfall": max(0.0, -cash),
    }
    timestamp = _now()
    with connect() as connection:
        connection.execute(
            """INSERT INTO fund_nav_snapshots (fund_id, snapshot_date, nav, aum, total_units, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fund_id, snapshot_date) DO UPDATE SET
                 nav = excluded.nav, aum = excluded.aum, total_units = excluded.total_units, updated_at = excluded.updated_at""",
            (fund_id, date.today().isoformat(), nav, aum, total_units, timestamp, timestamp),
        )
    return result


def _investor_statement_snapshot(user_id, fund_id, investor_id):
    """Build a transparent investor-level view from units and cash flows."""
    snapshot = _fund_snapshot(user_id, fund_id)
    if not snapshot:
        return None
    investor = next((row for row in snapshot["investors"] if row["id"] == investor_id), None)
    if not investor:
        return None
    transactions = [row for row in snapshot["transactions"] if row["investor_id"] == investor_id]
    transactions_chronological = list(reversed(transactions))
    investment_transactions = [
        row for row in transactions_chronological
        if row["transaction_type"] in {"ALLOCATION", "SUBSCRIPTION"}
    ]
    first_investment = investment_transactions[0] if investment_transactions else None
    invested = investor["subscribed"]
    redeemed = investor["redeemed"]
    current_value = investor["current_value"]
    net_gain = investor["gain"]
    net_return = (net_gain / invested * 100) if invested else 0.0
    with connect() as connection:
        nav_history = [dict(row) for row in connection.execute(
            "SELECT snapshot_date, nav, aum FROM fund_nav_snapshots WHERE fund_id = ? ORDER BY snapshot_date",
            (fund_id,),
        ).fetchall()]

    joined_nav = first_investment["nav"] if first_investment and first_investment.get("nav") else snapshot["fund"]["opening_nav"]

    def nav_return_since(cutoff):
        eligible = [row for row in nav_history if row["snapshot_date"] <= cutoff]
        if not eligible:
            return None
        base = eligible[-1]["nav"]
        return ((snapshot["nav"] / base) - 1) * 100 if base else None

    joined_return = ((snapshot["nav"] / joined_nav) - 1) * 100 if joined_nav else None
    today = date.today()
    activity = {
        "stock_purchases": snapshot["stock_cash_flows"]["purchases"],
        "stock_sales": snapshot["stock_cash_flows"]["sale_proceeds"],
        "purchase_count": snapshot["stock_cash_flows"]["purchase_count"],
        "sale_count": snapshot["stock_cash_flows"]["sale_count"],
        "fund_transactions": snapshot["transactions"][:12],
    }
    ownership_share = investor["ownership_pct"] / 100 if investor["ownership_pct"] else 0.0
    joined_on = parse_date(first_investment["transaction_date"]) if first_investment else None
    charge_years = {}
    brokerage = 0.0
    closed_trade_count = 0
    for position in snapshot["assets"]["positions"]:
        buy_date = parse_date(position["buy_date"])
        sell_date = parse_date(position["sell_date"])
        sell_price = position["sell_price"]
        qty = position["qty"] or 0
        buy_price = position["buy_price"] or 0
        if not buy_date or not sell_date or sell_price is None or not qty or joined_on and sell_date < joined_on:
            continue
        closed_trade_count += 1
        investor_turnover = (buy_price * qty + sell_price * qty) * ownership_share
        estimated_trade_brokerage = investor_turnover * 0.001  # 0.10% of buy + sell turnover.
        brokerage += estimated_trade_brokerage
        gain_after_brokerage = ((sell_price - buy_price) * qty * ownership_share) - estimated_trade_brokerage
        fiscal_year = f"{sell_date.year}-{str(sell_date.year + 1)[-2:]}" if sell_date.month >= 4 else f"{sell_date.year - 1}-{str(sell_date.year)[-2:]}"
        bucket = charge_years.setdefault(fiscal_year, {"stcg": 0.0, "ltcg": 0.0})
        if (sell_date - buy_date).days >= 365:
            bucket["ltcg"] += gain_after_brokerage
        else:
            bucket["stcg"] += gain_after_brokerage
    stcg_tax = sum(max(0.0, bucket["stcg"]) * 0.20 for bucket in charge_years.values())
    ltcg_tax = sum(max(0.0, bucket["ltcg"] - 125_000) * 0.125 for bucket in charge_years.values())
    charges = {
        "brokerage": brokerage,
        "stcg_tax": stcg_tax,
        "ltcg_tax": ltcg_tax,
        "total": brokerage + stcg_tax + ltcg_tax,
        "stcg_gain": sum(bucket["stcg"] for bucket in charge_years.values()),
        "ltcg_gain": sum(bucket["ltcg"] for bucket in charge_years.values()),
        "closed_trade_count": closed_trade_count,
        "ownership_share": ownership_share,
    }
    return {
        "fund": snapshot,
        "investor": investor,
        "transactions": transactions,
        "invested": invested,
        "redeemed": redeemed,
        "current_value": current_value,
        "net_gain": net_gain,
        "net_return": net_return,
        "joined_date": first_investment["transaction_date"] if first_investment else None,
        "joined_nav": joined_nav,
        "joined_return": joined_return,
        "one_month_return": nav_return_since((today - timedelta(days=30)).isoformat()),
        "ytd_return": nav_return_since(date(today.year, 1, 1).isoformat()),
        "nav_history": nav_history,
        "activity": activity,
        "charges": charges,
    }


@fund_bp.before_request
def fund_login_guard():
    return _require_login()


@fund_bp.get("")
def dashboard():
    with connect() as connection:
        first = connection.execute("SELECT id FROM funds WHERE user_id = ? ORDER BY id LIMIT 1", (_user_id(),)).fetchone()
    return redirect(url_for("fund_management.fund_dashboard", fund_id=first["id"])) if first else redirect(url_for("fund_management.manage_funds"))


@fund_bp.route("/funds", methods=["GET", "POST"])
def manage_funds():
    user_id = _user_id()
    with connect() as connection:
        owner_profiles = [dict(row) for row in connection.execute(
            "SELECT * FROM investor_profiles WHERE user_id = ? AND is_active = 1 ORDER BY name", (user_id,)
        ).fetchall()]
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            if not name:
                raise ValueError("Fund name is required.")
            opening_nav = _float(request.form.get("opening_nav") or 10, "Opening NAV", 0.01)
            owner_profile_id = int(request.form.get("owner_profile_id") or 0)
            owner_profile = next((row for row in owner_profiles if row["id"] == owner_profile_id), None)
            if not owner_profile:
                raise ValueError("Select a fund owner from Investor Management.")
            scope = (request.form.get("stock_scope") or "SWING").strip().upper()
            with connect() as connection:
                valid = scope == "ALL" or connection.execute(
                    "SELECT 1 FROM stock_trade_groups WHERE user_id = ? AND UPPER(code) = ?", (user_id, scope)
                ).fetchone()
                if not valid:
                    raise ValueError("Selected stock group was not found.")
                timestamp = _now()
                fund_id = connection.execute(
                    "INSERT INTO funds (user_id, name, opening_nav, seed_units, stock_scope, is_test_data, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                    (user_id, name, opening_nav, scope, 1 if request.form.get("is_test_data") == "on" else 0, timestamp, timestamp),
                ).lastrowid
            fund = _get_fund(user_id, fund_id)
            assets = _stock_assets(fund)
            if assets["market_value"] <= 0:
                with connect() as connection:
                    connection.execute("DELETE FROM funds WHERE id = ?", (fund_id,))
                raise ValueError("The assigned stock portfolio has no open market value to allocate to an owner.")
            seed_units = assets["market_value"] / opening_nav
            with connect() as connection:
                connection.execute("UPDATE funds SET seed_units = ? WHERE id = ?", (seed_units, fund_id))
                timestamp = _now()
                investor_id = connection.execute(
                    """INSERT INTO fund_investors
                       (user_id, fund_id, profile_id, name, email, phone, notes, is_test_data, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, fund_id, owner_profile_id, owner_profile["name"], owner_profile["email"], owner_profile["phone"],
                     owner_profile["notes"], owner_profile["is_test_data"], timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """INSERT INTO fund_transactions
                       (user_id, fund_id, investor_id, transaction_type, transaction_date, amount, nav, units, notes, created_at)
                       VALUES (?, ?, ?, 'ALLOCATION', ?, ?, ?, ?, ?, ?)""",
                    (user_id, fund_id, investor_id, date.today().isoformat(), assets["market_value"], opening_nav,
                     seed_units, "Fund owner opening allocation — 100%", timestamp),
                )
            flash(f"Fund {name} created.", "success")
            return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))
        except ValueError as exc:
            flash(str(exc), "error")
    with connect() as connection:
        funds = [dict(row) for row in connection.execute("SELECT * FROM funds WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()]
        groups = [dict(row) for row in connection.execute(
            "SELECT code, name FROM stock_trade_groups WHERE user_id = ? AND is_active = 1 ORDER BY display_order, name", (user_id,)
        ).fetchall()]
    cards = []
    for fund in funds:
        snapshot = _fund_snapshot(user_id, fund["id"])
        cards.append({**fund, "aum": snapshot["aum"], "nav": snapshot["nav"], "investor_count": len([i for i in snapshot["investors"] if i["is_active"]]), "open_bets": snapshot["assets"]["open_bets"]})
    return render_template("funds_manage.html", funds=cards, stock_groups=groups, owner_profiles=owner_profiles,
                           money=format_indian_money, title="Manage Funds")


@fund_bp.route("/investors", methods=["GET", "POST"])
def manage_investors():
    user_id = _user_id()
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            if not name:
                raise ValueError("Investor name is required.")
            timestamp = _now()
            with connect() as connection:
                connection.execute(
                    """INSERT INTO investor_profiles
                       (user_id, name, email, phone, notes, is_test_data, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, name, request.form.get("email") or None, request.form.get("phone") or None,
                     request.form.get("notes") or None, 1 if request.form.get("is_test_data") == "on" else 0, timestamp, timestamp),
                )
            flash(f"Investor {name} added to the investor directory.", "success")
            return redirect(url_for("fund_management.manage_investors"))
        except ValueError as exc:
            flash(str(exc), "error")
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT p.*, COUNT(i.id) AS fund_count FROM investor_profiles p
               LEFT JOIN fund_investors i ON i.profile_id = p.id AND i.is_active = 1
               WHERE p.user_id = ? GROUP BY p.id ORDER BY p.is_active DESC, p.name""", (user_id,)
        ).fetchall()]
    return render_template("investors_manage.html", investors=rows, title="Investor Management")


@fund_bp.route("/investors/<int:investor_id>/edit", methods=["GET", "POST"])
def edit_investor(investor_id):
    user_id = _user_id()
    with connect() as connection:
        row = connection.execute("SELECT * FROM investor_profiles WHERE id = ? AND user_id = ?", (investor_id, user_id)).fetchone()
    if not row:
        flash("Investor was not found.", "error")
        return redirect(url_for("fund_management.manage_investors"))
    investor = dict(row)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Investor name is required.", "error")
        else:
            with connect() as connection:
                connection.execute(
                    """UPDATE investor_profiles SET name = ?, email = ?, phone = ?, notes = ?, is_test_data = ?, updated_at = ?
                       WHERE id = ? AND user_id = ?""",
                    (name, request.form.get("email") or None, request.form.get("phone") or None,
                     request.form.get("notes") or None, 1 if request.form.get("is_test_data") == "on" else 0,
                     _now(), investor_id, user_id),
                )
            flash(f"Investor {name} updated.", "success")
            return redirect(url_for("fund_management.manage_investors"))
    return render_template("investor_edit.html", investor=investor, title=f"Edit {investor['name']}")


@fund_bp.post("/investors/<int:investor_id>/delete")
def delete_investor_profile(investor_id):
    user_id = _user_id()
    with connect() as connection:
        profile = connection.execute("SELECT * FROM investor_profiles WHERE id = ? AND user_id = ?", (investor_id, user_id)).fetchone()
        if not profile:
            flash("Investor was not found.", "error")
        elif connection.execute("SELECT COUNT(*) FROM fund_investors WHERE profile_id = ?", (investor_id,)).fetchone()[0]:
            flash("Remove this investor from every fund before deleting their directory record.", "error")
        else:
            connection.execute("DELETE FROM investor_profiles WHERE id = ? AND user_id = ?", (investor_id, user_id))
            flash(f"Investor {profile['name']} deleted from the directory.", "success")
    return redirect(url_for("fund_management.manage_investors"))


@fund_bp.post("/funds/<int:fund_id>/delete")
def delete_fund(fund_id):
    user_id = _user_id()
    fund = _get_fund(user_id, fund_id)
    if not fund:
        flash("Fund was not found.", "error")
    else:
        with connect() as connection:
            connection.execute("DELETE FROM fund_transactions WHERE fund_id = ? AND user_id = ?", (fund_id, user_id))
            connection.execute("DELETE FROM fund_investors WHERE fund_id = ? AND user_id = ?", (fund_id, user_id))
            connection.execute("DELETE FROM fund_position_baselines WHERE fund_id = ?", (fund_id,))
            connection.execute("DELETE FROM fund_nav_snapshots WHERE fund_id = ?", (fund_id,))
            connection.execute("DELETE FROM funds WHERE id = ? AND user_id = ?", (fund_id, user_id))
        flash(f"Fund {fund['name']} and its fund records were permanently deleted.", "success")
    return redirect(url_for("fund_management.manage_funds"))


@fund_bp.route("/funds/<int:fund_id>/edit", methods=["GET", "POST"])
def edit_fund(fund_id):
    user_id = _user_id()
    fund = _get_fund(user_id, fund_id)
    if not fund:
        flash("Fund was not found.", "error")
        return redirect(url_for("fund_management.manage_funds"))
    with connect() as connection:
        groups = [dict(row) for row in connection.execute(
            "SELECT code, name FROM stock_trade_groups WHERE user_id = ? AND is_active = 1 ORDER BY display_order, name",
            (user_id,),
        ).fetchall()]
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            if not name:
                raise ValueError("Fund name is required.")
            opening_nav = _float(request.form.get("opening_nav"), "Opening NAV", 0.01)
            scope = (request.form.get("stock_scope") or "SWING").strip().upper()
            if scope != "ALL" and not any(row["code"].upper() == scope for row in groups):
                raise ValueError("Selected stock group was not found.")
            scope_changed = scope != fund["stock_scope"]
            with connect() as connection:
                transaction_count = connection.execute(
                    "SELECT COUNT(*) FROM fund_transactions WHERE user_id = ? AND fund_id = ?", (user_id, fund_id)
                ).fetchone()[0]
                connection.execute(
                    """UPDATE funds SET name = ?, opening_nav = ?, stock_scope = ?, is_test_data = ?,
                       baseline_initialized = ?, updated_at = ? WHERE id = ? AND user_id = ?""",
                    (name, opening_nav, scope, 1 if request.form.get("is_test_data") == "on" else 0,
                     0 if scope_changed else fund["baseline_initialized"], _now(), fund_id, user_id),
                )
                if scope_changed:
                    connection.execute("DELETE FROM fund_position_baselines WHERE fund_id = ?", (fund_id,))
            updated = _get_fund(user_id, fund_id)
            if not transaction_count:
                assets = _stock_assets(updated)
                with connect() as connection:
                    connection.execute(
                        "UPDATE funds SET seed_units = ? WHERE id = ?",
                        (assets["market_value"] / opening_nav if assets["market_value"] else 0, fund_id),
                    )
            flash(f"Fund {name} updated.", "success")
            return redirect(url_for("fund_management.manage_funds"))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("fund_edit.html", fund=fund, stock_groups=groups, title=f"Edit {fund['name']}")


@fund_bp.get("/funds/<int:fund_id>")
def fund_dashboard(fund_id):
    snapshot = _fund_snapshot(_user_id(), fund_id)
    if not snapshot:
        flash("Fund was not found.", "error")
        return redirect(url_for("fund_management.manage_funds"))
    assigned = {row["profile_id"]: row for row in snapshot["investors"] if row["profile_id"]}
    with connect() as connection:
        investor_profiles = [dict(row) for row in connection.execute(
            "SELECT * FROM investor_profiles WHERE user_id = ? AND is_active = 1 ORDER BY name", (_user_id(),)
        ).fetchall()]
    for profile in investor_profiles:
        membership = assigned.get(profile["id"])
        profile["is_assigned"] = membership is not None
        profile["ownership_pct"] = membership["ownership_pct"] if membership else 0
    return render_template("fund_management.html", fund=snapshot, investor_profiles=investor_profiles, today=date.today().isoformat(),
                           money=format_indian_money, number=format_indian_decimal, title=snapshot["fund"]["name"])


@fund_bp.get("/funds/<int:fund_id>/investors/<int:investor_id>/statement")
def investor_statement(fund_id, investor_id):
    statement = _investor_statement_snapshot(_user_id(), fund_id, investor_id)
    if not statement:
        flash("Investor or fund was not found.", "error")
        return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))
    return render_template(
        "investor_statement.html",
        statement=statement,
        money=format_indian_money,
        number=format_indian_decimal,
        title=f"{statement['investor']['name']} Statement",
    )


@fund_bp.get("/funds/<int:fund_id>/growth-data")
def fund_growth_data(fund_id):
    user_id = _user_id()
    if not _get_fund(user_id, fund_id):
        return jsonify({"error": "Fund was not found."}), 404
    with connect() as connection:
        snapshots = [dict(row) for row in connection.execute(
            "SELECT snapshot_date, nav FROM fund_nav_snapshots WHERE fund_id = ? ORDER BY snapshot_date", (fund_id,)
        ).fetchall()]
    if not snapshots:
        return jsonify({"fund": [], "nifty": []})
    first_nav = snapshots[0]["nav"] or 1
    fund_points = [{"date": row["snapshot_date"], "value": round(row["nav"] / first_nav * 100, 2), "nav": round(row["nav"], 4)} for row in snapshots]
    nifty_points = []
    try:
        end_date = (date.today() + timedelta(days=1)).isoformat()
        history = nifty50_history(snapshots[0]["snapshot_date"], end_date)
        if history:
            first_close = history[0]["close"]
            nifty_points = [{"date": row["date"], "value": round(row["close"] / first_close * 100, 2), "close": row["close"]} for row in history]
    except Exception:
        # Benchmark data is supplementary; the fund chart remains available.
        nifty_points = []
    return jsonify({"fund": fund_points, "nifty": nifty_points})


@fund_bp.post("/funds/<int:fund_id>/settings")
def update_settings(fund_id):
    user_id = _user_id()
    try:
        fund_name = (request.form.get("fund_name") or "AK MultiCap Fund").strip()
        opening_nav = _float(request.form.get("opening_nav"), "Opening NAV", 0.01)
        current = _get_fund(user_id, fund_id)
        if not current:
            raise ValueError("Fund was not found.")
        assets = _stock_assets(current)
        with connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM fund_transactions WHERE user_id = ? AND fund_id = ?", (user_id, fund_id)).fetchone()[0]
            seed_units = current["seed_units"] if count else (assets["market_value"] / opening_nav if assets["market_value"] else 0)
            connection.execute("UPDATE funds SET name = ?, opening_nav = ?, seed_units = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                               (fund_name, opening_nav, seed_units, _now(), fund_id, user_id))
        flash("Fund settings updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


@fund_bp.post("/funds/<int:fund_id>/investors")
def add_investor(fund_id):
    user_id = _user_id()
    try:
        profile_id = int(request.form.get("profile_id") or 0)
        with connect() as connection:
            profile = connection.execute(
                "SELECT * FROM investor_profiles WHERE id = ? AND user_id = ? AND is_active = 1", (profile_id, user_id)
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM fund_investors WHERE fund_id = ? AND profile_id = ?", (fund_id, profile_id)
            ).fetchone()
        if not profile:
            raise ValueError("Select an investor from Investor Management.")
        name = profile["name"]
        mode = "funding"
        snapshot = _fund_snapshot(user_id, fund_id)
        if not snapshot:
            raise ValueError("Fund was not found.")
        allocation = None
        amount = _float(request.form.get("funding_amount"), "Funding amount", 0.01)
        nav = snapshot["nav"]
        allocation = ("SUBSCRIPTION", amount, nav, amount / nav, "Investor funding")
        timestamp = _now()
        with connect() as connection:
            investor_id = existing["id"] if existing else connection.execute(
                "INSERT INTO fund_investors (user_id, fund_id, profile_id, name, email, phone, notes, is_test_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, fund_id, profile_id, name, profile["email"], profile["phone"], profile["notes"], profile["is_test_data"], timestamp, timestamp),
            ).lastrowid
            if allocation:
                kind, amount, nav, units, notes = allocation
                connection.execute(
                    """INSERT INTO fund_transactions
                       (user_id, fund_id, investor_id, transaction_type, transaction_date, amount, nav, units, notes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, fund_id, investor_id, kind, request.form.get("transaction_date") or date.today().isoformat(),
                     amount, nav, units, notes, timestamp),
                )
        flash(f"Investor {name} added.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


def _record_transaction(user_id, fund_id, investor_id, kind, amount, nav, units, transaction_date, notes):
    with connect() as connection:
        connection.execute(
            """INSERT INTO fund_transactions
               (user_id, fund_id, investor_id, transaction_type, transaction_date, amount, nav, units, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, fund_id, investor_id, kind, transaction_date or date.today().isoformat(), amount, nav, units, notes or None, _now()),
        )


@fund_bp.post("/funds/<int:fund_id>/transactions")
def add_transaction(fund_id):
    user_id = _user_id()
    try:
        investor_id = int(request.form.get("investor_id") or 0)
        kind = "REDEMPTION"
        snapshot = _fund_snapshot(user_id, fund_id)
        investor = next((row for row in snapshot["investors"] if row["id"] == investor_id), None)
        if not investor:
            raise ValueError("Investor was not found.")
        nav = snapshot["nav"]
        units_text = (request.form.get("units") or "").strip()
        amount_text = (request.form.get("amount") or "").strip()
        units = _float(units_text, "Units", 0.000001) if units_text else None
        amount = _float(amount_text, "Amount", 0.01) if amount_text else None
        if units is None and amount is None:
            raise ValueError("Enter either an amount or units.")
        units = units if units is not None else amount / nav
        amount = amount if amount is not None else units * nav
        if kind == "REDEMPTION" and units > investor["units"] + 0.000001:
            raise ValueError("Redemption units exceed this investor's holdings.")
        _record_transaction(user_id, fund_id, investor_id, kind, amount, nav, units, request.form.get("transaction_date"), request.form.get("notes"))
        flash(f"{kind.title()} recorded for {investor['name']}.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


@fund_bp.post("/funds/<int:fund_id>/ownership-allocation")
def allocate_existing_ownership(fund_id):
    user_id = _user_id()
    try:
        investor_id = int(request.form.get("investor_id") or 0)
        target_pct = _float(request.form.get("target_ownership_pct"), "Target ownership percentage", 0.01)
        if target_pct > 100:
            raise ValueError("Target ownership percentage cannot exceed 100%.")
        snapshot = _fund_snapshot(user_id, fund_id)
        investor = next((row for row in snapshot["investors"] if row["id"] == investor_id and row["is_active"]), None)
        if not investor:
            raise ValueError("Active investor was not found.")
        target_units = snapshot["total_units"] * target_pct / 100
        units = target_units - investor["units"]
        if units <= 0.000001:
            raise ValueError(f"{investor['name']} already owns at least {target_pct:.2f}% of the fund.")
        if units > snapshot["unallocated_units"] + 0.000001:
            available_pct = snapshot["unallocated_units"] / snapshot["total_units"] * 100 if snapshot["total_units"] else 0
            raise ValueError(f"Only {available_pct:.2f}% remains unallocated.")
        _record_transaction(
            user_id, fund_id, investor_id, "ALLOCATION", units * snapshot["nav"], snapshot["nav"], units,
            request.form.get("transaction_date"), request.form.get("notes") or f"Ownership assigned to target {target_pct:.2f}%",
        )
        flash(f"{investor['name']} now owns {target_pct:.2f}% of the fund.", "success")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


@fund_bp.post("/funds/<int:fund_id>/investors/<int:investor_id>/remove")
def remove_investor(fund_id, investor_id):
    user_id = _user_id()
    snapshot = _fund_snapshot(user_id, fund_id)
    investor = next((row for row in snapshot["investors"] if row["id"] == investor_id), None)
    if not investor:
        flash("Investor was not found.", "error")
        return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))
    if investor["is_test_data"]:
        with connect() as connection:
            connection.execute("DELETE FROM fund_transactions WHERE user_id = ? AND fund_id = ? AND investor_id = ?", (user_id, fund_id, investor_id))
            connection.execute("DELETE FROM fund_investors WHERE id = ? AND user_id = ? AND fund_id = ?", (investor_id, user_id, fund_id))
        flash(f"Test investor {investor['name']} and all related test transactions were permanently deleted.", "success")
        return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))
    if abs(investor["units"]) > 0.000001:
        flash(f"{investor['name']} still owns {investor['units']:.6f} units. Redeem all units before removing the investor.", "error")
        return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))

    with connect() as connection:
        transaction_count = connection.execute(
            "SELECT COUNT(*) FROM fund_transactions WHERE user_id = ? AND fund_id = ? AND investor_id = ?",
            (user_id, fund_id, investor_id),
        ).fetchone()[0]
        if transaction_count:
            connection.execute(
                "UPDATE fund_investors SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ? AND fund_id = ?",
                (_now(), investor_id, user_id, fund_id),
            )
            flash(f"{investor['name']} was archived. Transaction history was preserved.", "success")
        else:
            connection.execute("DELETE FROM fund_investors WHERE id = ? AND user_id = ? AND fund_id = ?", (investor_id, user_id, fund_id))
            flash(f"{investor['name']} was removed.", "success")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


@fund_bp.post("/funds/<int:fund_id>/investors/purge-test-data")
def purge_test_investors(fund_id):
    user_id = _user_id()
    with connect() as connection:
        ids = [row["id"] for row in connection.execute(
            "SELECT id FROM fund_investors WHERE user_id = ? AND fund_id = ? AND is_test_data = 1", (user_id, fund_id)
        ).fetchall()]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            connection.execute(f"DELETE FROM fund_transactions WHERE user_id = ? AND investor_id IN ({placeholders})", [user_id, *ids])
            connection.execute(f"DELETE FROM fund_investors WHERE user_id = ? AND id IN ({placeholders})", [user_id, *ids])
    flash(f"Permanently deleted {len(ids)} test investor{'s' if len(ids) != 1 else ''} and their transactions.", "success")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


@fund_bp.post("/funds/<int:fund_id>/cash")
def add_cash_movement(fund_id):
    user_id = _user_id()
    try:
        kind = (request.form.get("transaction_type") or "").upper()
        if kind not in {"CASH_IN", "CASH_OUT"}:
            raise ValueError("Select cash inflow or cash deployed/outflow.")
        amount = _float(request.form.get("amount"), "Amount", 0.01)
        _record_transaction(user_id, fund_id, None, kind, amount, None, 0, request.form.get("transaction_date"), request.form.get("notes"))
        flash("Fund cash movement recorded.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("fund_management.fund_dashboard", fund_id=fund_id))


def register_fund_management(app):
    init_fund_db()
    app.register_blueprint(fund_bp)
