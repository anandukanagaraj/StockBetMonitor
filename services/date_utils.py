from datetime import date, datetime


def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def swap_day_month(value):
    try:
        return date(value.year, value.day, value.month)
    except (TypeError, ValueError):
        return None


def normalize_mixed_date(value, previous_date=None, today=None):
    parsed = parse_date(value)
    if parsed is None:
        return None

    today = today or date.today()
    swapped = swap_day_month(parsed)
    if swapped is None:
        return parsed

    if parsed > today and swapped <= today:
        return swapped

    if previous_date and parsed < previous_date and previous_date <= swapped <= today:
        return swapped

    return parsed


def position_sort_key(position):
    try:
        if isinstance(position, dict):
            return int(position.get("bet_no") or 0)
        return int(position["bet_no"] or 0)
    except (KeyError, TypeError, ValueError):
        try:
            return int(getattr(position, "bet_no") or 0)
        except (TypeError, ValueError):
            return 0


def get_value(position, key):
    if isinstance(position, dict):
        return position.get(key)
    try:
        return position[key]
    except (KeyError, TypeError):
        return getattr(position, key)


def position_identity(position):
    if isinstance(position, dict):
        return position.get("id") or position.get("bet_no") or id(position)
    try:
        return position["id"] or position["bet_no"] or id(position)
    except (KeyError, TypeError):
        return getattr(position, "id", None) or getattr(position, "bet_no", None) or id(position)


def normalized_buy_date_map(positions, today=None):
    normalized = {}
    previous = None
    for position in sorted(positions, key=position_sort_key):
        value = normalize_mixed_date(get_value(position, "buy_date"), previous, today=today)
        normalized[position_identity(position)] = value
        if value is not None:
            previous = value
    return normalized


def display_date(value):
    parsed = parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else (value or "-")


def iso_date(value):
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else (value or None)
