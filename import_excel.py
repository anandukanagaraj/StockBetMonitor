import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from db import connect, default_stock_group_id, now_iso
from services.date_utils import normalized_buy_date_map, position_identity


DEFAULT_EXCEL = Path(__file__).resolve().parents[1] / "StockMonitor" / "data" / "Portfolio.xlsx"


def normalize_header(value):
    return str(value or "").strip().lower()


def normalize_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def normalize_number(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value == "":
            return None
    return float(value)


def normalize_int(value):
    number = normalize_number(value)
    return int(number) if number is not None else None


def find_header_row(sheet):
    for row_number in range(1, min(sheet.max_row, 10) + 1):
        values = [normalize_header(sheet.cell(row_number, col).value) for col in range(1, sheet.max_column + 1)]
        if "bet count" in values:
            return row_number
    raise ValueError("Could not find Excel header row with 'Bet Count'.")


def read_positions(excel_path):
    workbook = load_workbook(excel_path, data_only=True)
    sheet = workbook.active
    header_row = find_header_row(sheet)
    headers = {
        normalize_header(sheet.cell(header_row, col).value): col
        for col in range(1, sheet.max_column + 1)
        if sheet.cell(header_row, col).value is not None
    }

    def cell(row_number, column_name):
        column = headers.get(column_name)
        if column is None:
            return None
        return sheet.cell(row_number, column).value

    positions = []
    for row_number in range(header_row + 1, sheet.max_row + 1):
        ticker = cell(row_number, "ticker")
        status = cell(row_number, "status")
        if ticker in (None, "") or status in (None, ""):
            continue

        positions.append(
            {
                "bet_no": normalize_int(cell(row_number, "bet count")),
                "ticker": str(ticker).strip().upper(),
                "buy_date": normalize_date(cell(row_number, "buy date")),
                "qty": normalize_number(cell(row_number, "qty")),
                "buy_price": normalize_number(cell(row_number, "buy price")),
                "target_price": normalize_number(cell(row_number, "target price")),
                "stop_loss": normalize_number(cell(row_number, "stop loss")),
                "current_price": normalize_number(cell(row_number, "current price")),
                "sell_date": normalize_date(cell(row_number, "sell date")),
                "sell_price": normalize_number(cell(row_number, "sell price")),
                "status": str(status).strip(),
                "notes": None,
            }
        )

    normalized_buy_dates = normalized_buy_date_map(positions)
    for position in positions:
        normalized = normalized_buy_dates.get(position_identity(position))
        if normalized is not None:
            position["buy_date"] = normalized.isoformat()

    workbook.close()
    return positions


def import_positions(positions):
    timestamp = now_iso()
    imported = 0
    skipped = 0

    with connect() as connection:
        trade_group_id = default_stock_group_id(connection)
        for row in positions:
            if not row["ticker"] or not row["buy_date"] or row["qty"] is None or row["buy_price"] is None:
                skipped += 1
                continue

            try:
                connection.execute(
                    """
                    INSERT INTO positions (
                        bet_no, trade_group_id, ticker, buy_date, qty, buy_price, target_price, stop_loss,
                        current_price, sell_date, sell_price, status, notes, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_group_id, bet_no) DO UPDATE SET
                        ticker = excluded.ticker,
                        buy_date = excluded.buy_date,
                        qty = excluded.qty,
                        buy_price = excluded.buy_price,
                        target_price = excluded.target_price,
                        stop_loss = excluded.stop_loss,
                        current_price = excluded.current_price,
                        sell_date = excluded.sell_date,
                        sell_price = excluded.sell_price,
                        status = excluded.status,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["bet_no"],
                        trade_group_id,
                        row["ticker"],
                        row["buy_date"],
                        row["qty"],
                        row["buy_price"],
                        row["target_price"],
                        row["stop_loss"],
                        row["current_price"],
                        row["sell_date"],
                        row["sell_price"],
                        row["status"],
                        row["notes"],
                        timestamp,
                        timestamp,
                    ),
                )
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1

    return imported, skipped


def main():
    parser = argparse.ArgumentParser(description="Import V1 Excel bets into the V2 SQLite database.")
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL), help="Path to Portfolio.xlsx")
    args = parser.parse_args()

    excel_path = Path(args.excel)
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    from services.schema import ensure_schema

    ensure_schema()
    positions = read_positions(excel_path)
    imported, skipped = import_positions(positions)
    print(f"Read {len(positions)} rows from {excel_path}")
    print(f"Imported or updated {imported} rows")
    print(f"Skipped {skipped} rows")


if __name__ == "__main__":
    main()
