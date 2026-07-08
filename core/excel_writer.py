from pathlib import Path
from openpyxl import load_workbook


class ExcelWriter:

    def __init__(self, excel_file: str):

        self.excel_file = Path(excel_file)

    def update_prices(self, positions):

        workbook = load_workbook(self.excel_file)
        sheet = workbook.active

        # -----------------------------
        # Find header row
        # -----------------------------
        header_row = None

        for r in range(1, min(sheet.max_row, 10) + 1):

            values = []

            for c in range(1, sheet.max_column + 1):

                value = sheet.cell(r, c).value

                if value is None:
                    values.append("")
                else:
                    values.append(str(value).strip().lower())

            if "bet count" in values:
                header_row = r
                break

        if header_row is None:
            raise Exception("Header row not found.")

        headers = {}

        print("\nDetected Columns")
        for name, col in headers.items():
            print(col, "->", name)
            
        for c in range(1, sheet.max_column + 1):

            value = sheet.cell(header_row, c).value

            if value is not None:
                headers[str(value).strip().lower()] = c

        bet_col = headers["bet count"]
        current_price_col = headers["current price"]
        
        

        # -----------------------------
        # Update prices
        # -----------------------------
        price_map = {}

        for p in positions:
            price_map[int(p.bet_no)] = p.current_price

        updated = 0

        for row in range(header_row + 1, sheet.max_row + 1):

            bet = sheet.cell(row=row, column=bet_col).value

            if bet is None:
                continue

            try:
                bet = int(bet)
            except Exception:
                continue

            if bet not in price_map:
                continue

            sheet.cell(
                row=row,
                column=current_price_col
            ).value = round(price_map[bet], 2)

            updated += 1

        workbook.save(self.excel_file)
        workbook.close()

        return updated