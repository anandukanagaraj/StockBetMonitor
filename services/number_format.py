def format_indian_number(value, decimals=2):
    if value is None:
        return None

    number = float(value)
    sign = "-" if number < 0 else ""
    abs_number = abs(number)

    if decimals is None:
        integer_part = str(int(round(abs_number)))
        decimal_part = None
    else:
        formatted = f"{abs_number:.{decimals}f}"
        integer_part, decimal_part = formatted.split(".")

    if len(integer_part) <= 3:
        grouped = integer_part
    else:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while remaining:
            groups.append(remaining[-2:])
            remaining = remaining[:-2]
        groups.reverse()
        grouped = ",".join(groups + [last_three])

    if decimal_part is not None:
        return f"{sign}{grouped}.{decimal_part}"
    return f"{sign}{grouped}"


def format_indian_money(value):
    if value is None:
        return "-"
    formatted = format_indian_number(value, 2)
    if formatted.startswith("-"):
        return f"-₹{formatted[1:]}"
    return f"₹{formatted}"


def format_indian_money_signed(value):
    if value is None:
        return "-"
    number = float(value)
    body = format_indian_number(abs(number), 2)
    if number > 0:
        return f"+₹{body}"
    if number < 0:
        return f"-₹{body}"
    return f"₹{body}"


def format_indian_inr(value):
    if value is None:
        return "-"
    formatted = format_indian_number(value, 2)
    if formatted.startswith("-"):
        return f"INR -{formatted[1:]}"
    return f"INR {formatted}"


def format_indian_compact_money(value):
    if value is None:
        return "-"
    number = float(value)
    sign = "-" if number < 0 else ""
    abs_value = abs(number)
    if abs_value >= 10_000_000:
        return f"{sign}₹{abs_value / 10_000_000:.2f}Cr"
    if abs_value >= 100_000:
        return f"{sign}₹{abs_value / 100_000:.2f}L"
    if abs_value >= 1_000:
        return f"{sign}₹{abs_value / 1_000:.2f}k"
    return format_indian_money(value)


def format_indian_decimal(value):
    if value is None:
        return "-"
    number = float(value)
    if number.is_integer():
        return format_indian_number(number, decimals=None)
    formatted = format_indian_number(number, 2).rstrip("0").rstrip(".")
    return formatted
