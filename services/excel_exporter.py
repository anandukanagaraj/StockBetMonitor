from copy import copy
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from services.date_utils import normalized_buy_date_map, parse_date, position_identity


BASE_DIR = Path(__file__).resolve().parents[1]
SOURCE_TEMPLATE = BASE_DIR.parent / "StockMonitor" / "data" / "Portfolio.xlsx"
EXPORT_DIR = BASE_DIR / "exports"


def excel_date_text(value):
    parsed = parse_date(value)
    if parsed:
        return parsed.strftime("%d/%m/%Y")
    return value or None


def copy_row_format(sheet, source_row, target_row, start_col=1, end_col=None):
    end_col = end_col or sheet.max_column
    sheet.row_dimensions[target_row].height = sheet.row_dimensions[source_row].height
    for col in range(start_col, end_col + 1):
        source = sheet.cell(source_row, col)
        target = sheet.cell(target_row, col)
        if source.has_style:
            target._style = copy(source._style)
        if source.number_format:
            target.number_format = source.number_format
        if source.font:
            target.font = copy(source.font)
        if source.fill:
            target.fill = copy(source.fill)
        if source.border:
            target.border = copy(source.border)
        if source.alignment:
            target.alignment = copy(source.alignment)


def write_position(sheet, row_number, position, normalized_buy_date=None):
    qty = position["qty"] or 0
    buy_price = position["buy_price"] or 0
    sell_price = position["sell_price"]
    target_price = position["target_price"]
    stop_loss = position["stop_loss"]

    sheet.cell(row_number, 2).value = position["bet_no"]
    sheet.cell(row_number, 3).value = excel_date_text(normalized_buy_date or position["buy_date"])
    sheet.cell(row_number, 4).value = excel_date_text(position["sell_date"])
    sheet.cell(row_number, 5).value = position["ticker"]
    sheet.cell(row_number, 6).value = qty
    sheet.cell(row_number, 7).value = buy_price
    sheet.cell(row_number, 8).value = f"=G{row_number}*F{row_number}"
    sheet.cell(row_number, 9).value = target_price
    sheet.cell(row_number, 10).value = stop_loss
    sheet.cell(row_number, 11).value = position["current_price"]
    sheet.cell(row_number, 12).value = sell_price
    sheet.cell(row_number, 13).value = f"=(I{row_number}-G{row_number})*F{row_number}" if target_price is not None else None
    sheet.cell(row_number, 14).value = f"=(J{row_number}-G{row_number})*F{row_number}" if stop_loss is not None else None
    sheet.cell(row_number, 15).value = f"=(L{row_number}-G{row_number})*F{row_number}" if sell_price is not None else None
    sheet.cell(row_number, 16).value = position["status"] or "Open"


def export_positions_to_excel(positions):
    if not SOURCE_TEMPLATE.exists():
        raise FileNotFoundError(f"Source Excel template not found: {SOURCE_TEMPLATE}")

    workbook = load_workbook(SOURCE_TEMPLATE)
    sheet = workbook.active
    sheet.title = "Bets"
    start_row = 5
    template_row = 5
    positions = sorted(
        positions,
        key=lambda row: (
            row["bet_no"] is None,
            row["bet_no"] or 0,
            row["id"] or 0,
        ),
    )
    normalized_buy_dates = normalized_buy_date_map(positions)

    required_last_row = start_row + max(len(positions), 1) - 1
    if required_last_row > sheet.max_row:
        for row_number in range(sheet.max_row + 1, required_last_row + 1):
            copy_row_format(sheet, template_row, row_number, 1, sheet.max_column)

    for row_number in range(start_row, max(sheet.max_row, required_last_row) + 1):
        copy_row_format(sheet, template_row, row_number, 1, sheet.max_column)
        for col in range(2, 17):
            sheet.cell(row_number, col).value = None

    for offset, position in enumerate(positions):
        write_position(
            sheet,
            start_row + offset,
            position,
            normalized_buy_date=normalized_buy_dates.get(position_identity(position)),
        )

    EXPORT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = EXPORT_DIR / f"portfolio_export_{timestamp}.xlsx"
    workbook.save(output_path)
    workbook.close()
    return output_path
