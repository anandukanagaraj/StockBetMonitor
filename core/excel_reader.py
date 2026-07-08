from pathlib import Path
from typing import List

from openpyxl import load_workbook

from core.models import Position


class ExcelReader:

    def __init__(self, excel_file: str):

        self.excel_file = Path(excel_file)

        if not self.excel_file.exists():
            raise FileNotFoundError(f"Excel file not found: {self.excel_file}")

    def load_positions(self, open_only: bool = True) -> List[Position]:

        workbook = load_workbook(self.excel_file, data_only=False)
        sheet = workbook.active
        
        # --------------------------------------------------
        # Find header row automatically
        # --------------------------------------------------

        header_row = None

        for r in range(1, min(sheet.max_row, 10) + 1):

            values = []

            for c in range(1, sheet.max_column + 1):

                value = sheet.cell(row=r, column=c).value

                if value is None:
                    values.append("")
                else:
                    values.append(str(value).strip().lower())

            if "bet count" in values:
                header_row = r
                break

        if header_row is None:
            raise Exception(
                "Could not locate the Excel header row. "
                "Expected a column named 'Bet Count'."
            )

        # --------------------------------------------------
        # Read headers
        # --------------------------------------------------

        headers = {}

        for c in range(1, sheet.max_column + 1):

            value = sheet.cell(row=header_row, column=c).value

            if value is not None:
                headers[str(value).strip().lower()] = c

        required = [
            "bet count",
            "ticker",
            "qty",
            "buy price",
            "trade capital",
            "target price",
            "status",
        ]

        missing = [x for x in required if x not in headers]

        if missing:
            raise Exception(
                f"Missing required columns: {', '.join(missing)}"
            )

        # --------------------------------------------------
        # Helper
        # --------------------------------------------------

        def cell(row_number, column_name):

            column = headers.get(column_name.lower())

            if column is None:
                return None

            return sheet.cell(row=row_number, column=column).value

        # --------------------------------------------------
        # Read positions
        # --------------------------------------------------

        positions = []
        for row in range(header_row + 1, sheet.max_row + 1):

            status = cell(row, "status")

            if status is None:
                continue

            status = str(status).strip()

            if open_only and status.lower() != "open":
                continue

            positions.append(
                Position(
                    bet_no=cell(row, "bet count"),
                    buy_date=cell(row, "buy date"),
                    sell_date=cell(row, "sell date"),
                    ticker=str(cell(row, "ticker")).strip(),
                    qty=cell(row, "qty"),
                    buy_price=cell(row, "buy price"),
                    trade_capital=cell(row, "trade capital"),
                    target_price=cell(row, "target price"),
                    stop_loss=cell(row, "stop loss"),
                    current_price=cell(row, "current price"),
                    sell_price=cell(row, "sell price"),
                    target_profit=cell(row, "target profit"),
                    target_loss=cell(row, "target loss"),
                    achieved_pl=cell(row, "achieved p/l"),
                    status=status,
                )
            )

        workbook.close()

        return positions
