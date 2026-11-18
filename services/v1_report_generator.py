import base64
from datetime import date, datetime
from html import escape
import json
from pathlib import Path

from services.date_utils import (
    normalize_mixed_date as normalize_shared_mixed_date,
    normalized_buy_date_map,
    parse_date as parse_shared_date,
    position_identity,
)
from services.number_format import (
    format_indian_decimal,
    format_indian_money,
    format_indian_number,
)

GROWW_STOCK_PATHS = {}
try:
    GROWW_STOCK_PATHS = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "groww_stock_slugs.json").read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError):
    pass


class ReportGenerator:

    def generate_html(self, positions, show_target_price=False, include_app_nav=False):

        Path("reports").mkdir(exist_ok=True)

        generated_at = datetime.now()
        report_kind = (
            "app_targets"
            if include_app_nav and show_target_price
            else "app_masked"
            if include_app_nav
            else "targets"
            if show_target_price
            else "masked"
        )
        filename = f"reports/report_{report_kind}_{generated_at.strftime('%Y%m%d_%H%M%S')}.html"
        logo_path = Path("static/logo-mark.png")
        logo_data_uri = ""
        if logo_path.exists():
            logo_data_uri = "data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode("ascii")

        def position_status(position):
            return (position.status or "").strip().lower()

        open_positions = [
            p
            for p in positions
            if position_status(p) == "open"
        ]
        closed_positions = [
            p
            for p in positions
            if position_status(p) != "open"
        ]

        def value_as_float(value):
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            value = str(value).strip()
            if not value or value.startswith("="):
                return None
            try:
                return float(value.replace(",", "").replace("INR", "").strip())
            except ValueError:
                return None

        priced_open_positions = [
            p
            for p in open_positions
            if value_as_float(p.current_price) is not None
        ]

        def profit(position):
            current_price = value_as_float(position.current_price)
            buy_price = value_as_float(position.buy_price)
            qty = value_as_float(position.qty) or 0
            if current_price is None or buy_price is None:
                return None
            return (current_price - buy_price) * qty

        def profit_percent(position):
            current_price = value_as_float(position.current_price)
            buy_price = value_as_float(position.buy_price)
            if current_price is None or not buy_price:
                return None
            return ((current_price - buy_price) / buy_price) * 100

        def remaining_to_target(position):
            target_price = value_as_float(position.target_price)
            current_price = value_as_float(position.current_price)
            if not target_price or current_price is None:
                return None
            return target_price - current_price

        def closed_profit(position):
            achieved_pl = value_as_float(position.achieved_pl)
            if achieved_pl is not None:
                return achieved_pl

            sell_price = value_as_float(position.sell_price)
            buy_price = value_as_float(position.buy_price)
            qty = value_as_float(position.qty) or 0
            if sell_price is None or buy_price is None:
                return None
            return (sell_price - buy_price) * qty

        def closed_profit_percent(position):
            buy_price = value_as_float(position.buy_price)
            sell_price = value_as_float(position.sell_price)

            if not buy_price:
                return None

            if sell_price is not None:
                return ((sell_price - buy_price) / buy_price) * 100

            value = closed_profit(position)
            invested = buy_price * (value_as_float(position.qty) or 0)
            if not invested or value is None:
                return None

            return value / invested * 100

        def parse_date(value):
            return parse_shared_date(value)

        def invested_value(position):
            return (value_as_float(position.buy_price) or 0) * (value_as_float(position.qty) or 0)

        def target_value(position):
            target_price = value_as_float(position.target_price)
            buy_price = value_as_float(position.buy_price) or 0
            qty = value_as_float(position.qty) or 0
            return (target_price if target_price is not None else buy_price) * qty

        winners = sorted(priced_open_positions, key=profit_percent, reverse=True)[:5]
        losers = sorted(priced_open_positions, key=profit_percent)[:5]

        near_target = []
        for p in priced_open_positions:
            target_price = value_as_float(p.target_price)
            current_price = value_as_float(p.current_price)

            if not target_price or current_price is None:
                continue

            remaining_pct = (target_price - current_price) / target_price * 100
            if current_price >= value_as_float(p.buy_price) and remaining_pct <= 5:
                near_target.append(p)
        near_target = sorted(
            near_target,
            key=lambda p: (
                (value_as_float(p.target_price) - value_as_float(p.current_price))
                / value_as_float(p.target_price)
                * 100
                if value_as_float(p.target_price) and value_as_float(p.current_price) is not None
                else 999
            ),
        )[:5]

        total_invested = sum(
            (value_as_float(p.buy_price) or 0) * (value_as_float(p.qty) or 0)
            for p in open_positions
        )
        current_value = sum(
            (value_as_float(p.current_price) or 0) * (value_as_float(p.qty) or 0)
            for p in open_positions
        )
        total_profit = current_value - total_invested
        total_profit_pct = (total_profit / total_invested * 100) if total_invested else 0
        closed_profit_total = sum(
            value
            for value in (closed_profit(p) for p in closed_positions)
            if value is not None
        )
        open_expected_return = sum(
            target_value(p) - invested_value(p)
            for p in open_positions
        )
        expected_total_return = open_expected_return
        realized_return_pct = (
            closed_profit_total / total_invested * 100
            if total_invested
            else None
        )

        winners_count = len(
            [
                p
                for p in priced_open_positions
                if (value_as_float(p.current_price) or 0) >= (value_as_float(p.buy_price) or 0)
            ]
        )
        losers_count = len(priced_open_positions) - winners_count
        closed_winners_count = len(
            [p for p in closed_positions if (closed_profit(p) or 0) > 0]
        )
        closed_losers_count = len(
            [p for p in closed_positions if (closed_profit(p) or 0) < 0]
        )
        closed_return_pcts = [
            value
            for value in (closed_profit_percent(position) for position in closed_positions)
            if value is not None
        ]
        avg_realized_return = (
            sum(closed_return_pcts) / len(closed_return_pcts)
            if closed_return_pcts
            else None
        )
        total_wins = closed_winners_count
        closed_count = len(closed_positions)
        outcome_win_rate = (
            closed_winners_count / closed_count * 100
            if closed_count
            else None
        )
        outcome_loss_rate = (
            closed_losers_count / closed_count * 100
            if closed_count
            else 0
        )
        outcome_neutral_rate = max(0, 100 - (outcome_win_rate or 0) - outcome_loss_rate)
        win_label = "win" if total_wins == 1 else "wins"
        loss_label = "loss" if closed_losers_count == 1 else "losses"
        first_bet_buy_date = min(
            [
                parsed_date
                for parsed_date in (parse_date(position.buy_date) for position in positions)
                if parsed_date is not None
            ],
            default=None,
        )
        winning_hold_days = []
        winning_sell_dates = []
        for position in closed_positions:
            if (closed_profit(position) or 0) <= 0:
                continue

            buy_date = parse_date(position.buy_date)
            sell_date = parse_date(position.sell_date)
            if buy_date is None or sell_date is None or sell_date < buy_date:
                continue

            winning_hold_days.append((sell_date - buy_date).days)
            winning_sell_dates.append(sell_date)

        avg_winning_hold_days = (
            sum(winning_hold_days) / len(winning_hold_days)
            if winning_hold_days
            else None
        )
        winning_sell_dates = sorted(winning_sell_dates)
        winner_gap_start_dates = (
            [first_bet_buy_date] + winning_sell_dates[:-1]
            if first_bet_buy_date is not None and winning_sell_dates
            else winning_sell_dates[:-1]
        )
        winner_gap_days = [
            (current_date - previous_date).days
            for previous_date, current_date in zip(winner_gap_start_dates, winning_sell_dates)
            if previous_date is not None and current_date >= previous_date
        ]
        avg_winner_gap_days = (
            sum(winner_gap_days) / len(winner_gap_days)
            if winner_gap_days
            else None
        )

        def money(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            return format_indian_money(value)

        def compact_money(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            abs_value = abs(value)
            sign = "-" if value < 0 else ""
            if abs_value >= 1_000_000_000_000:
                return f"{sign}â‚¹{abs_value:.2e}"
            if abs_value >= 10_000_000:
                return f"{sign}₹{abs_value / 10_000_000:.2f}Cr"
            if abs_value >= 100_000:
                return f"{sign}₹{abs_value / 100_000:.2f}L"
            return format_indian_money(value)

        def number(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            return format_indian_number(value, 2)

        def compact_number(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            abs_value = abs(value)
            sign = "-" if value < 0 else ""
            if abs_value >= 1_000_000_000_000:
                return f"{sign}{abs_value:.2e}"
            if abs_value >= 10_000_000:
                return f"{sign}{abs_value / 10_000_000:.2f}Cr"
            if abs_value >= 100_000:
                return f"{sign}{abs_value / 100_000:.2f}L"
            return format_indian_number(value, 2)

        def qty(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            if value.is_integer():
                return format_indian_number(value, decimals=None)
            return format_indian_decimal(value)

        def pct(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            if abs(value) >= 10_000:
                return f"{value:+.2e}%"
            return f"{value:+.2f}%"

        def rate(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            if abs(value) >= 10_000:
                return f"{value:.2e}%"
            return f"{value:.2f}%"

        def days(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            label = "day" if round(value) == 1 else "days"
            return f"{value:.0f} {label}"

        def text(value):
            if value is None:
                return "-"
            return escape(str(value))

        def groww_stock_link(position):
            ticker = str(position.ticker or "").strip()
            if not ticker:
                return text(position.ticker)
            groww_path = GROWW_STOCK_PATHS.get(ticker.upper())
            if not groww_path:
                return text(position.ticker)
            href = f"https://groww.in{groww_path}"
            return f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{text(position.ticker)}</a>'

        def display_number(value):
            return f'<span class="compact-value" title="{number(value)}">{compact_number(value)}</span>'

        def display_money(value):
            return f'<span class="compact-value" title="{money(value)}">{compact_money(value)}</span>'

        def format_date(parsed_date, fallback_value):
            if parsed_date is None:
                return text(fallback_value)
            return parsed_date.strftime("%d/%m/%Y")

        def display_date(value):
            return format_date(parse_date(value), value)

        def sort_date(value):
            parsed_date = parse_date(value)
            if parsed_date is None:
                return text(value)
            return parsed_date.isoformat()

        def normalize_mixed_date(value, previous_date=None):
            return normalize_shared_mixed_date(value, previous_date, today=generated_at.date())

        normalized_buy_dates = normalized_buy_date_map(positions, today=generated_at.date())

        def buy_date_for(position):
            return normalized_buy_dates.get(position_identity(position)) or parse_date(position.buy_date)

        def display_buy_date(position):
            return format_date(buy_date_for(position), position.buy_date)

        def sort_buy_date(position):
            parsed_date = buy_date_for(position)
            if parsed_date is None:
                return text(position.buy_date)
            return parsed_date.isoformat()

        def pl_class(value):
            value = value_as_float(value)
            if value is None:
                return "neutral"
            if value > 0:
                return "positive"
            if value < 0:
                return "negative"
            return "neutral"

        def status_for(position):
            target_price = value_as_float(position.target_price)
            current_price = value_as_float(position.current_price)

            if current_price is None:
                return "Price Missing", "neutral-pill"

            if target_price and current_price is not None and current_price >= target_price:
                return "Target Hit", "success"

            remaining = remaining_to_target(position)
            if target_price and remaining is not None:
                remaining_pct = remaining / target_price * 100
                if current_price >= value_as_float(position.buy_price) and remaining_pct <= 5:
                    return "Near Target", "warning"

            return "Hold", "info"

        def open_status_priority(position):
            """Order the daily report by actionability before performance."""
            status_label, _ = status_for(position)
            return {
                "Target Hit": 0,
                "Near Target": 1,
                "Hold": 2,
                "Price Missing": 3,
            }.get(status_label, 4)

        def table_rows(rows):
            rendered = []

            for p in rows:
                row_profit = profit(p)
                row_profit_pct = profit_percent(p)
                status_label, status_class = status_for(p)

                rendered.append(
                    f"""
                    <tr>
                        <td data-sort-value="{text(p.ticker).lower()}">
                            <strong>{groww_stock_link(p)}</strong>
                            <span class="subtle">Bet {text(p.bet_no)}</span>
                        </td>
                        <td data-sort-value="{sort_buy_date(p)}">{display_buy_date(p)}</td>
                        <td>{qty(p.qty)}</td>
                        <td data-sort-value="{value_as_float(p.buy_price) or 0}">{display_number(p.buy_price)}</td>
                        <td data-sort-value="{value_as_float(p.current_price) or 0}">{display_number(p.current_price)}</td>
                        <td data-sort-value="{value_as_float(p.target_price) or 0}">{display_number(p.target_price) if show_target_price else '<span class="pill neutral-pill">Locked</span>'}</td>
                        <td class="{pl_class(row_profit)}" data-sort-value="{row_profit if row_profit is not None else 0}">{display_money(row_profit)}</td>
                        <td class="{pl_class(row_profit_pct)}" data-sort-value="{row_profit_pct if row_profit_pct is not None else 0}">{pct(row_profit_pct)}</td>
                        <td data-sort-value="{open_status_priority(p)}"><span class="pill {status_class}">{status_label}</span></td>
                    </tr>
                    """
                )

            return "\n".join(rendered)

        def closed_table_rows(rows):
            rendered = []

            for p in rows:
                row_profit = closed_profit(p)
                row_profit_pct = closed_profit_percent(p)
                status_label = text(p.status).upper()

                rendered.append(
                    f"""
                    <tr>
                        <td data-sort-value="{text(p.ticker).lower()}">
                            <strong>{groww_stock_link(p)}</strong>
                            <span class="subtle">Bet {text(p.bet_no)}</span>
                        </td>
                        <td data-sort-value="{sort_buy_date(p)}">{display_buy_date(p)}</td>
                        <td>{qty(p.qty)}</td>
                        <td data-sort-value="{value_as_float(p.buy_price) or 0}">{display_number(p.buy_price)}</td>
                        <td data-sort-value="{value_as_float(p.sell_price) or 0}">{display_number(p.sell_price)}</td>
                        <td class="{pl_class(row_profit)}" data-sort-value="{row_profit if row_profit is not None else 0}">{display_money(row_profit)}</td>
                        <td class="{pl_class(row_profit_pct)}" data-sort-value="{row_profit_pct if row_profit_pct is not None else 0}">{pct(row_profit_pct)}</td>
                        <td data-sort-value="{sort_date(p.sell_date)}">{display_date(p.sell_date)}</td>
                        <td><span class="pill neutral-pill">{status_label}</span></td>
                    </tr>
                    """
                )

            return "\n".join(rendered)

        def portfolio_value(position):
            qty = value_as_float(position.qty) or 0
            if position_status(position) == "open":
                price = value_as_float(position.current_price)
            else:
                price = value_as_float(position.sell_price) or value_as_float(position.current_price)

            if price is None:
                price = value_as_float(position.buy_price) or 0

            return price * qty

        def sort_key(position):
            bet_no = value_as_float(position.bet_no)
            if bet_no is not None:
                return bet_no
            return 0

        def portfolio_chart_data(rows):
            chart_rows = [p for p in sorted(rows, key=sort_key) if value_as_float(p.qty)]
            chart_events = []
            cumulative_invested = 0
            cumulative_realized_profit = 0
            points = []

            for position in chart_rows:
                buy_date = buy_date_for(position)
                chart_events.append((position, buy_date, "buy"))
                if position_status(position) != "open":
                    sell_date = normalize_mixed_date(position.sell_date, buy_date) or buy_date
                    chart_events.append((position, sell_date, "realized"))

            def event_rank(item):
                position, _, event_type = item
                if event_type == "buy":
                    return 0
                realized_change = closed_profit(position) or 0
                return 1 if realized_change >= 0 else 2

            chart_events.sort(
                key=lambda item: (
                    item[1] or date.max,
                    event_rank(item),
                    str(item[0].ticker or "").casefold(),
                    sort_key(item[0]),
                )
            )

            for position, point_date, event_type in chart_events:
                position_buy_date = buy_date_for(position)
                position_sell_date = normalize_mixed_date(position.sell_date, position_buy_date)
                if event_type == "buy":
                    cumulative_invested += invested_value(position)
                realized_change = 0
                if event_type == "realized":
                    realized_change = closed_profit(position) or 0
                    cumulative_realized_profit += realized_change

                event_label = "Bet added" if event_type == "buy" else "Profit realized"

                points.append(
                    {
                        "label": str(position.ticker or ""),
                        "betNo": position.bet_no,
                        "date": point_date.strftime("%Y-%m-%d") if point_date else "",
                        "buyDate": position_buy_date.strftime("%Y-%m-%d") if position_buy_date else str(position.buy_date or ""),
                        "sellDate": position_sell_date.strftime("%Y-%m-%d") if position_sell_date else str(position.sell_date or ""),
                        "holdingDays": (position_sell_date - position_buy_date).days if position_sell_date and position_buy_date else None,
                        "eventType": event_type,
                        "isClosedBet": position_status(position) != "open",
                        "invested": round(cumulative_invested, 2),
                        "investedAmount": round(invested_value(position), 2) if event_type == "buy" else 0,
                        "realized": round(cumulative_realized_profit, 2),
                        "realizedChange": round(realized_change, 2),
                        "eventLabel": event_label,
                        "qty": value_as_float(position.qty) or 0,
                        "buyPrice": value_as_float(position.buy_price),
                        "sellPrice": value_as_float(position.sell_price),
                        "targetPrice": value_as_float(position.target_price) if show_target_price else None,
                    }
                )

            return escape(json.dumps(points), quote=True)

        def ranking_rows(rows, value_getter, value_formatter, value_class=None):
            if not rows:
                return """
                    <tr>
                        <td colspan="3" class="empty">No bets to show.</td>
                    </tr>
                    <tr class="placeholder-row"><td class="rank">2</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">3</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">4</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">5</td><td>-</td><td>-</td></tr>
                """

            rendered = []
            for index, position in enumerate(rows, start=1):
                value = value_getter(position)
                css_class = value_class(value) if value_class else ""
                rendered.append(
                    f"""
                    <tr>
                        <td class="rank">{index}</td>
                        <td><strong>{groww_stock_link(position)}</strong></td>
                        <td class="{css_class}">{value_formatter(value)}</td>
                    </tr>
                    """
                )

            for index in range(len(rows) + 1, 6):
                rendered.append(
                    f"""
                    <tr class="placeholder-row">
                        <td class="rank">{index}</td>
                        <td>-</td>
                        <td>-</td>
                    </tr>
                    """
                )

            return "\n".join(rendered)

        def near_target_tooltip(position, status_label):
            current_price = value_as_float(position.current_price)
            target_price = value_as_float(position.target_price)
            lines = [
                f"Status: {status_label}",
                f"Current: {number(current_price)}",
            ]
            if show_target_price and target_price:
                remaining = target_price - current_price if current_price is not None else None
                remaining_pct = remaining / target_price * 100 if remaining is not None else None
                lines.extend(
                    [
                        f"Target: {number(target_price)}",
                        f"Gap: {number(remaining)} ({pct(remaining_pct)})",
                    ]
                )
            return escape("\n".join(lines), quote=True)

        def near_target_rows(rows):
            if not rows:
                return """
                    <tr>
                        <td colspan="3" class="empty">No bets to show.</td>
                    </tr>
                    <tr class="placeholder-row"><td class="rank">2</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">3</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">4</td><td>-</td><td>-</td></tr>
                    <tr class="placeholder-row"><td class="rank">5</td><td>-</td><td>-</td></tr>
                """

            rendered = []
            for index, position in enumerate(rows, start=1):
                status_label, status_class = status_for(position)
                rendered.append(
                    f"""
                    <tr title="{near_target_tooltip(position, status_label)}">
                        <td class="rank">{index}</td>
                        <td>
                            <strong>{groww_stock_link(position)}</strong>
                            <span class="subtle">Bet {text(position.bet_no)}</span>
                        </td>
                        <td><span class="pill {status_class}">{status_label}</span></td>
                    </tr>
                    """
                )

            for index in range(len(rows) + 1, 6):
                rendered.append(
                    f"""
                    <tr class="placeholder-row">
                        <td class="rank">{index}</td>
                        <td>-</td>
                        <td>-</td>
                    </tr>
                    """
                )

            return "\n".join(rendered)

        open_positions_by_pl_pct = sorted(
            open_positions,
            key=lambda position: (
                open_status_priority(position),
                profit_percent(position) is None,
                -(profit_percent(position) or 0),
                text(position.ticker).lower(),
            ),
        )

        portfolio_rows = (
            table_rows(open_positions_by_pl_pct)
            if open_positions
            else '<tr><td colspan="9" class="empty">No open bets with current prices.</td></tr>'
        )

        closed_rows = (
            closed_table_rows(closed_positions)
            if closed_positions
            else '<tr><td colspan="9" class="empty">No closed bets yet.</td></tr>'
        )
        # Flask injects templates/_navigation.html for application reports.
        # Keeping navigation out of snapshots prevents it drifting from base.html.
        app_nav_html = ""
        live_report_refresh_html = (
            """
        <a class="hero-refresh-action" href="/report-targets?refresh=1" title="Refresh live prices and rebuild both stock reports">
            <span class="hero-refresh-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" focusable="false">
                    <path d="M20 6v5h-5"></path>
                    <path d="M4 18v-5h5"></path>
                    <path d="M18.4 9A7 7 0 0 0 6.7 5.8L4 8.3"></path>
                    <path d="M5.6 15A7 7 0 0 0 17.3 18.2L20 15.7"></path>
                </svg>
            </span>
            <span>Refresh live data</span>
        </a>
"""
            if include_app_nav and show_target_price
            else ""
        )
        live_table_search_html = (
            """
            <label class="report-table-search">
                <span class="sr-only">Search this table</span>
                <input type="search" data-table-search placeholder="Search ticker or bet" autocomplete="off">
            </label>
"""
            if include_app_nav and show_target_price
            else ""
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Monitor Report</title>
<link rel="icon" href="{logo_data_uri or '/static/favicon.png'}" type="image/png">
<style>
:root {{
    color-scheme: light;
    --bg: #eef3f8;
    --panel: #ffffff;
    --ink: #172033;
    --muted: #64748b;
    --line: #dce5ef;
    --brand: #2563eb;
    --brand-dark: #1e3a8a;
    --positive: #087f5b;
    --positive-bg: #e7f8ef;
    --negative: #c92a2a;
    --negative-bg: #fff0f0;
    --warning: #b76e00;
    --warning-bg: #fff4d6;
    --info: #1d4ed8;
    --info-bg: #e8f0ff;
    --shadow: 0 20px 50px rgba(21, 32, 51, 0.10);
}}

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    min-height: 100vh;
    background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.14), transparent 34rem),
        linear-gradient(180deg, #f8fbff 0%, var(--bg) 100%);
    color: var(--ink);
    font-family: Inter, "Segoe UI", Arial, sans-serif;
}}

.app-header {{
    position: sticky;
    top: 0;
    z-index: 50;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 16px 32px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
}}

.brand {{
    display: inline-flex;
    align-items: center;
    gap: 12px;
    color: var(--ink);
    text-decoration: none;
}}

.brand-mark {{
    display: inline-flex;
    width: 42px;
    height: 42px;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    border-radius: 8px;
    box-shadow: 0 8px 20px rgba(3, 105, 161, .18);
}}

.brand-mark img {{
    display: block;
    width: 42px;
    height: 42px;
}}

.brand strong {{
    display: block;
    color: var(--ink);
    font-size: 17px;
    line-height: 1.2;
}}

.app-eyebrow {{
    display: block;
    margin: 0 0 4px;
    color: var(--brand);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
}}

.nav-actions {{
    position: relative;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 14px;
    justify-content: flex-end;
}}

.nav-form {{
    margin: 0;
}}

.nav-actions a:not(.button) {{
    color: #334155;
    font-weight: 700;
    text-decoration: none;
}}

.nav-menu {{
    position: relative;
}}

.nav-menu summary {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 0;
    color: #334155;
    cursor: pointer;
    font-weight: 700;
    list-style: none;
    user-select: none;
}}

.nav-menu summary::-webkit-details-marker {{
    display: none;
}}

.nav-menu summary::after {{
    content: "";
    width: 7px;
    height: 7px;
    border-right: 2px solid #64748b;
    border-bottom: 2px solid #64748b;
    transform: rotate(45deg) translateY(-2px);
}}

.nav-menu[open] summary {{
    color: var(--brand);
}}

.nav-dropdown {{
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 60;
    display: grid;
    min-width: 220px;
    padding: 8px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: #ffffff;
    box-shadow: 0 18px 42px rgba(15, 23, 42, .16);
}}

.reports-menu .nav-dropdown {{
    right: 0;
    left: auto;
}}

.account-menu .nav-dropdown {{
    right: 0;
    left: auto;
}}

.account-dropdown {{
    width: 280px;
}}

.account-name {{
    padding: 8px 12px 4px;
    color: var(--muted);
    font-weight: 700;
}}

.account-password-form {{
    display: grid;
    gap: 8px;
    padding: 8px;
}}

.account-password-form input {{
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    font: inherit;
}}

.account-password-form button {{
    padding: 9px 10px;
    border: 1px solid var(--brand);
    border-radius: 6px;
    background: var(--brand);
    color: #fff;
    cursor: pointer;
    font: inherit;
    font-weight: 700;
}}

.nav-dropdown a {{
    display: block;
    padding: 10px 12px;
    border-radius: 6px;
    color: #334155;
    font-weight: 700;
    text-decoration: none;
    white-space: nowrap;
}}

.nav-dropdown a:hover {{
    background: #f0f9ff;
    color: var(--brand);
}}

.link-button {{
    border: 0;
    background: transparent;
    color: #334155;
    cursor: pointer;
    font: inherit;
    font-weight: 700;
    padding: 0;
}}

.link-button:hover {{
    color: var(--brand);
}}

.nav-actions a,
.nav-menu summary,
.link-button {{
    font-size: 13px;
    white-space: nowrap;
}}

html {{
    -webkit-text-size-adjust: 100%;
}}

.page {{
    width: min(1220px, calc(100% - 32px));
    margin: 0 auto;
    padding: 32px 0 44px;
}}

.hero {{
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(420px, 0.95fr);
    align-items: stretch;
    gap: 24px;
    padding: 28px;
    border: 1px solid rgba(255, 255, 255, 0.75);
    border-radius: 8px;
    background: linear-gradient(135deg, var(--brand-dark), #0f766e);
    color: #ffffff;
    box-shadow: var(--shadow);
}}

.hero-refresh-action {{
    position: absolute;
    top: 18px;
    right: 18px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 36px;
    padding: 5px 12px 5px 6px;
    border: 1px solid rgba(255, 255, 255, .34);
    border-radius: 999px;
    background: rgba(255, 255, 255, .16);
    color: #ffffff;
    font-size: 12px;
    font-weight: 800;
    text-decoration: none;
    backdrop-filter: blur(10px);
    box-shadow: 0 12px 28px rgba(2, 6, 23, .18);
}}

.hero-refresh-action:hover {{
    border-color: rgba(255, 255, 255, .72);
    background: rgba(255, 255, 255, .24);
}}

.hero-refresh-icon {{
    display: inline-flex;
    width: 26px;
    height: 26px;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: #0ea5e9;
    color: #ffffff;
}}

.hero-refresh-icon svg {{
    width: 17px;
    height: 17px;
    fill: none;
    stroke: currentColor;
    stroke-width: 2.3;
    stroke-linecap: round;
    stroke-linejoin: round;
}}

.hero-copy {{
    min-width: 0;
}}

.report-brand {{
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 16px;
}}

.report-logo {{
    width: 46px;
    height: 46px;
    flex: 0 0 46px;
    border-radius: 10px;
    box-shadow: 0 10px 24px rgba(2, 6, 23, .20);
}}

.report-brand-copy strong {{
    display: block;
    color: #ffffff;
    font-size: 18px;
    line-height: 1.15;
}}

.report-brand-copy span {{
    display: block;
    margin-top: 3px;
    color: #bfdbfe;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .10em;
    text-transform: uppercase;
}}

.eyebrow {{
    margin: 0 0 8px;
    color: #bfdbfe;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
}}

h1 {{
    margin: 0;
    font-size: 34px;
    line-height: 1.1;
}}

.hero-meta {{
    margin: 10px 0 0;
    color: #dbeafe;
}}

.hero-metrics {{
    display: grid;
    grid-template-columns: repeat(2, minmax(210px, 1fr));
    gap: 12px;
    align-content: end;
}}

.hero-profit {{
    min-width: 0;
    padding: 18px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.12);
    text-align: right;
}}

.report-footer {{
    margin-top: 22px;
    padding: 14px 18px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: #ffffff;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.45;
}}

.report-footer strong {{
    color: #334155;
}}

.hero-profit span {{
    display: block;
    color: #dbeafe;
    font-size: 13px;
}}

.hero-profit strong {{
    display: block;
    margin-top: 6px;
    color: #ffffff;
    font-size: clamp(22px, 2.1vw, 28px);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    line-height: 1.12;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 16px;
    margin: 20px 0;
}}

.card {{
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    min-height: 122px;
    padding: 18px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel);
    box-shadow: 0 10px 28px rgba(21, 32, 51, 0.06);
}}

.card h2 {{
    margin: 0;
    color: var(--muted);
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
}}

.card p {{
    margin: 10px 0 0;
    min-height: 32px;
    font-size: clamp(19px, 1.35vw, 25px);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
    line-height: 1.18;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.card-note {{
    display: block;
    margin-top: 8px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
}}

.outcome-bar {{
    display: flex;
    height: 9px;
    margin-top: 10px;
    overflow: hidden;
    border-radius: 999px;
    background: #e2e8f0;
}}

.outcome-segment {{
    min-width: 0;
}}

.outcome-win {{
    background: linear-gradient(90deg, #16a34a, #22c55e);
}}

.outcome-loss {{
    background: linear-gradient(90deg, #ef4444, #dc2626);
}}

.outcome-neutral {{
    background: #cbd5e1;
}}

.grid {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: stretch;
    gap: 16px;
    margin-bottom: 20px;
}}

.grid .section {{
    display: flex;
    flex-direction: column;
    height: 100%;
}}

.grid .section + .section {{
    margin-top: 0;
}}

.grid .table-wrap {{
    flex: 1;
}}

.grid table {{
    height: 100%;
}}

.section {{
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel);
    box-shadow: 0 10px 28px rgba(21, 32, 51, 0.06);
}}

.section + .section {{
    margin-top: 20px;
}}

.section h2 {{
    margin: 0;
    padding: 18px 20px;
    border-bottom: 1px solid var(--line);
    font-size: 18px;
}}

.section-table-heading {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    border-bottom: 1px solid var(--line);
}}

.section-table-heading h2 {{
    flex: 1;
    border-bottom: 0;
}}

.report-table-search {{
    display: flex;
    align-items: center;
    padding-right: 16px;
}}

.report-table-search input {{
    width: 220px;
    max-width: 30vw;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: #fff;
    color: var(--text);
    font: inherit;
    font-size: 12px;
}}

.report-table-search input:focus {{
    border-color: #38bdf8;
    outline: 2px solid rgba(14, 165, 233, .18);
}}

.sr-only {{
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
}}

.table-wrap {{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

.interactive-table {{
    table-layout: fixed;
}}

th {{
    padding: 12px 14px;
    background: #f8fafc;
    color: #475569;
    font-size: 12px;
    letter-spacing: 0.06em;
    text-align: right;
    text-transform: uppercase;
    white-space: nowrap;
}}

th:first-child,
td:first-child {{
    text-align: left;
}}

td {{
    padding: 14px;
    border-top: 1px solid var(--line);
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}}

.interactive-table th,
.interactive-table td {{
    overflow: hidden;
    text-overflow: ellipsis;
}}

.compact-value {{
    display: inline-block;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    vertical-align: bottom;
}}

td.positive,
td.negative,
td.neutral {{
    min-width: 110px;
}}

tbody tr:hover {{
    background: #f8fbff;
}}

.placeholder-row td {{
    color: #cbd5e1;
    font-weight: 700;
}}

.subtle {{
    display: block;
    margin-top: 4px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 500;
}}

.positive {{
    color: var(--positive);
    font-weight: 800;
}}

.negative {{
    color: var(--negative);
    font-weight: 800;
}}

.neutral {{
    color: var(--muted);
    font-weight: 800;
}}

.hero-profit .positive {{
    color: #bbf7d0;
}}

.hero-profit .negative {{
    color: #fecaca;
}}

.hero-profit .neutral {{
    color: #e2e8f0;
}}

.hero-profit strong.positive,
.hero-profit strong.negative,
.hero-profit strong.neutral {{
    color: #ffffff;
}}

.value-chart {{
    padding: 24px;
}}

.value-chart h2 {{
    padding: 0;
    border-bottom: 0;
    font-size: 22px;
    font-weight: 700;
}}

.chart-top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
}}

.chart-stats {{
    display: flex;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 24px;
    margin-top: 26px;
}}

.chart-stat {{
    flex: 1 1 160px;
    min-width: 150px;
    max-width: 260px;
}}

.chart-stat + .chart-stat {{
    padding-left: 24px;
    border-left: 1px solid var(--line);
}}

.chart-stat span {{
    display: block;
    color: var(--muted);
    font-size: 13px;
}}

.chart-stat strong {{
    display: block;
    margin-top: 4px;
    color: #020617;
    font-size: 21px;
    line-height: 1.1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.chart-context {{
    align-items: center;
    color: var(--muted);
    display: flex;
    flex-wrap: wrap;
    font-size: 13px;
    font-weight: 700;
    gap: 12px 18px;
    margin: 18px 0 -6px;
}}

.chart-subtitle {{
    color: #334155;
}}

.chart-legend {{
    align-items: center;
    color: var(--muted);
    display: flex;
    flex-wrap: wrap;
    font-size: 11px;
    font-weight: 700;
    gap: 10px 12px;
    justify-content: flex-end;
    margin: 4px 72px 0 0;
}}

.legend-item {{
    align-items: center;
    display: inline-flex;
    gap: 6px;
}}

.legend-dot {{
    background: #ffffff;
    border: 2px solid #0284c7;
    border-radius: 999px;
    display: inline-block;
    height: 7px;
    width: 7px;
}}

.legend-dot.closed-buy-dot {{
    background: #22d3ee;
    border-color: #0e7490;
}}

.legend-star {{
    color: #f59e0b;
    font-size: 12px;
    line-height: 1;
}}

.legend-loss {{
    color: var(--negative);
    font-size: 12px;
    line-height: 1;
}}

.period-controls {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}}

.period-button,
.pagination button {{
    border: 1px solid transparent;
    border-radius: 999px;
    background: #edf1f5;
    color: var(--ink);
    cursor: pointer;
    font: inherit;
    font-size: 14px;
    font-weight: 700;
    padding: 7px 13px;
}}

.period-button.active,
.pagination button:not(:disabled):hover {{
    border-color: #0284c7;
    background: #e0f2fe;
    color: #075985;
}}

.period-button:hover {{
    border-color: #7dd3fc;
}}

.line-chart {{
    display: block;
    width: 100%;
    height: auto;
    margin-top: 26px;
    overflow: visible;
}}

.axis {{
    stroke: #94a3b8;
    stroke-width: 1.2;
}}

.zero-axis {{
    stroke: #94a3b8;
    stroke-dasharray: 5 5;
    stroke-width: 1;
}}

.chart-area {{
    fill: rgba(14, 165, 233, 0.09);
}}

.chart-line {{
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 2.6;
}}

.chart-line.realized {{
    stroke: #0284c7;
}}

.line-chart circle {{
    fill: #ffffff;
    stroke: #0284c7;
    stroke-width: 2.6;
}}

.line-chart circle.bet-added {{
    stroke: #0284c7;
}}

.line-chart circle.bet-added.bet-closed {{
    fill: #22d3ee;
    stroke: #0e7490;
}}

.event-star {{
    fill: #f59e0b;
    stroke: #b45309;
    stroke-width: 1.8;
}}

.event-loss {{
    fill: var(--negative);
    stroke: var(--negative);
    stroke-width: 1.5;
}}

.chart-label {{
    fill: var(--muted);
    font-size: 12px;
    font-weight: 700;
}}

.chart-label.end {{
    text-anchor: end;
}}

.chart-label.axis-right {{
    text-anchor: start;
}}

th.sortable {{
    cursor: pointer;
    user-select: none;
}}

th.sortable::after {{
    content: "  sort";
    color: #94a3b8;
    font-size: 10px;
    letter-spacing: 0;
    text-transform: none;
}}

th.sorted-asc::after {{
    content: "  up";
    color: var(--brand);
}}

th.sorted-desc::after {{
    content: "  down";
    color: var(--brand);
}}

.pagination {{
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    padding: 14px 20px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 13px;
    font-weight: 700;
}}

.pagination button:disabled {{
    cursor: not-allowed;
    opacity: 0.45;
}}

.pill {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 96px;
    padding: 6px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 800;
}}

.pill.success {{
    background: var(--positive-bg);
    color: var(--positive);
}}

.pill.warning {{
    background: var(--warning-bg);
    color: var(--warning);
}}

.pill.info {{
    background: var(--info-bg);
    color: var(--info);
}}

.pill.neutral-pill {{
    background: #f1f5f9;
    color: #475569;
}}

.rank {{
    width: 52px;
    color: var(--muted);
    font-weight: 800;
    text-align: center;
}}

.empty {{
    padding: 24px;
    color: var(--muted);
    text-align: center;
}}

@media (max-width: 900px) {{
    .hero {{
        align-items: stretch;
        grid-template-columns: 1fr;
        padding: 22px;
    }}

    .hero-refresh-action {{
        top: 16px;
        right: 16px;
    }}

    .hero-profit {{
        text-align: left;
    }}

    .hero-metrics {{
        grid-template-columns: 1fr;
    }}

    .cards,
    .grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }}

    .chart-top {{
        align-items: flex-start;
        flex-direction: column;
    }}

    .chart-stats {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
        width: 100%;
        margin-top: 18px;
    }}

    .chart-stat {{
        min-width: 0;
    }}

    .chart-stat + .chart-stat {{
        padding-left: 12px;
    }}
}}

@media (max-width: 1020px) {{
    .app-header {{
        align-items: flex-start;
        flex-direction: column;
        padding: 20px;
    }}

    .nav-actions {{
        align-items: flex-start;
        flex-wrap: wrap;
    }}

    .nav-dropdown,
    .reports-menu .nav-dropdown {{
        right: auto;
        left: 0;
    }}
}}

@media (max-width: 640px) {{
    .app-header {{
        position: relative;
        align-items: flex-start;
        flex-direction: column;
        gap: 12px;
        padding: 14px 16px;
    }}

    .nav-actions {{
        width: 100%;
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 10px 14px;
    }}

    .nav-dropdown {{
        right: auto;
        left: 0;
    }}

    .reports-menu .nav-dropdown {{
        right: auto;
        left: 0;
    }}

    .page {{
        width: 100%;
        padding: 10px 10px 28px;
    }}

    h1 {{
        font-size: 26px;
    }}

    .hero {{
        padding: 18px;
        border-radius: 0;
    }}

    .hero-refresh-action {{
        position: static;
        justify-self: flex-start;
        width: max-content;
        margin-bottom: 4px;
    }}

    .hero-meta {{
        font-size: 13px;
    }}

    .hero-metrics {{
        gap: 10px;
    }}

    .hero-profit {{
        min-width: 0;
        padding: 14px;
    }}

    .hero-profit strong {{
        font-size: 23px;
    }}

    .cards,
    .grid {{
        grid-template-columns: 1fr;
    }}

    .cards {{
        gap: 10px;
        margin: 12px 0;
    }}

    .card {{
        min-height: 0;
        padding: 14px;
    }}

    .card p {{
        min-height: 0;
        font-size: 22px;
    }}

    .section {{
        border-left: 0;
        border-right: 0;
        border-radius: 0;
    }}

    .section + .section {{
        margin-top: 12px;
    }}

    .section h2 {{
        padding: 14px 12px;
        font-size: 17px;
    }}

    .value-chart {{
        padding: 16px 10px 18px;
    }}

    .value-chart h2 {{
        font-size: 19px;
    }}

    .period-controls {{
        width: 100%;
        gap: 6px;
    }}

    .period-button,
    .pagination button {{
        min-height: 34px;
        padding: 6px 11px;
        font-size: 13px;
    }}

    .chart-stats {{
        grid-template-columns: 1fr;
        gap: 10px;
    }}

    .chart-stat + .chart-stat {{
        padding-left: 0;
        padding-top: 10px;
        border-left: 0;
        border-top: 1px solid var(--line);
    }}

    .chart-stat strong {{
        font-size: 19px;
    }}

    .line-chart {{
        min-height: 210px;
        margin-top: 16px;
    }}

    .chart-label {{
        font-size: 11px;
    }}

    th,
    td {{
        padding: 11px 10px;
        font-size: 13px;
    }}

    .subtle {{
        font-size: 11px;
    }}

    .pill {{
        min-width: 82px;
        padding: 5px 8px;
        font-size: 11px;
    }}

    .pagination {{
        align-items: stretch;
        flex-direction: column;
        padding: 12px;
    }}
}}
</style>
</head>
<body>
{app_nav_html}
<main class="page">
    <section class="hero">
        {live_report_refresh_html}
        <div class="hero-copy">
            <div class="report-brand">
                <img class="report-logo" src="{logo_data_uri or '/static/logo-mark.png'}" alt="">
                <div class="report-brand-copy">
                    <strong>My Portfolio Bets</strong>
                    <span>Retail Bet Platform</span>
                </div>
            </div>
            <p class="eyebrow">Daily Portfolio Report</p>
            <h1>Stock Monitor</h1>
            <p class="hero-meta">Generated {generated_at.strftime("%d-%b-%Y at %I:%M %p")}</p>
        </div>
        <div class="hero-metrics">
            <div class="hero-profit">
                <span>Total investment</span>
                <strong title="{money(total_invested)}">{compact_money(total_invested)}</strong>
                <span>Open trades capital</span>
            </div>
            <div class="hero-profit">
                <span>Realized profit</span>
                <strong class="{pl_class(closed_profit_total)}" title="{money(closed_profit_total)}">{compact_money(closed_profit_total)}</strong>
                <span>{rate(realized_return_pct)} realized return</span>
            </div>
        </div>
    </section>

    <section class="cards" aria-label="Summary">
        <article class="card">
            <h2>Total Bets</h2>
            <p>{len(positions)}</p>
        </article>
        <article class="card">
            <h2>Open</h2>
            <p>{len(open_positions)}</p>
        </article>
        <article class="card">
            <h2>Closed</h2>
            <p>{len(closed_positions)}</p>
        </article>
        <article class="card">
            <h2>Outcome Rate</h2>
            <p class="{pl_class(outcome_win_rate)}">{rate(outcome_win_rate) if outcome_win_rate is not None else "-"}</p>
            <span class="card-note">{closed_winners_count} {win_label} &middot; {closed_losers_count} {loss_label} &middot; {closed_count} closed</span>
            <div class="outcome-bar" title="{rate(outcome_win_rate) if outcome_win_rate is not None else '0.00%'} wins / {rate(outcome_loss_rate)} losses">
                <span class="outcome-segment outcome-win" style="width: {outcome_win_rate or 0:.2f}%"></span>
                <span class="outcome-segment outcome-loss" style="width: {outcome_loss_rate:.2f}%"></span>
                <span class="outcome-segment outcome-neutral" style="width: {outcome_neutral_rate:.2f}%"></span>
            </div>
        </article>
        <article class="card">
            <h2>Avg Realized Return</h2>
            <p class="{pl_class(avg_realized_return)}">{pct(avg_realized_return)}</p>
            <span class="card-note">Average P/L % of {len(closed_return_pcts)} closed bets</span>
        </article>
        <article class="card">
            <h2>Winner Timing</h2>
            <p>{days(avg_winning_hold_days)}</p>
            <span class="card-note">Avg hold &middot; winners every ~{days(avg_winner_gap_days)}</span>
        </article>
    </section>

    <section class="section value-chart">
        <div class="chart-top">
            <h2>Realized Profit</h2>
            <div class="period-controls" aria-label="Chart period">
                <button class="period-button" data-period="1m" type="button">1M</button>
                <button class="period-button" data-period="3m" type="button">3M</button>
                <button class="period-button" data-period="6m" type="button">6M</button>
                <button class="period-button" data-period="ytd" type="button">YTD</button>
                <button class="period-button" data-period="1y" type="button">1Y</button>
                <button class="period-button" data-period="2y" type="button">2Y</button>
                <button class="period-button active" data-period="all" type="button">All</button>
            </div>
        </div>
        <div id="portfolio-chart" data-points='{portfolio_chart_data(positions)}'></div>
    </section>

    <section class="grid">
        <article class="section">
            <h2>Top 5 Winners</h2>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr><th>Rank</th><th>Ticker</th><th>P/L %</th></tr>
                    </thead>
                    <tbody>
                        {ranking_rows(winners, profit_percent, pct, pl_class)}
                    </tbody>
                </table>
            </div>
        </article>

        <article class="section">
            <h2>Top 5 Losers</h2>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr><th>Rank</th><th>Ticker</th><th>P/L %</th></tr>
                    </thead>
                    <tbody>
                        {ranking_rows(losers, profit_percent, pct, pl_class)}
                    </tbody>
                </table>
            </div>
        </article>

        <article class="section">
            <h2>Near Target</h2>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr><th>Rank</th><th>Ticker</th><th>Status</th></tr>
                    </thead>
                    <tbody>
                        {near_target_rows(near_target)}
                    </tbody>
                </table>
            </div>
        </article>
    </section>

    <section class="section">
        <div class="section-table-heading"><h2>Open Bets</h2>{live_table_search_html}</div>
        <div class="table-wrap">
            <table class="interactive-table" data-page-size="10">
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Buy Date</th>
                        <th>Qty</th>
                        <th>Buy</th>
                        <th>Current</th>
                        <th>Target</th>
                        <th>P/L</th>
                        <th>P/L %</th>
                        <th class="sorted-asc">Status</th>
                    </tr>
                </thead>
                <tbody>
                    {portfolio_rows}
                </tbody>
            </table>
        </div>
        <div class="pagination" data-pagination></div>
    </section>

    <section class="section">
        <div class="section-table-heading"><h2>Closed Bets</h2>{live_table_search_html}</div>
        <div class="table-wrap">
            <table class="interactive-table" data-page-size="10">
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Buy Date</th>
                        <th>Qty</th>
                        <th>Buy</th>
                        <th>Sell</th>
                        <th>Realized P/L</th>
                        <th>P/L %</th>
                        <th>Sell Date</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {closed_rows}
                </tbody>
            </table>
        </div>
        <div class="pagination" data-pagination></div>
    </section>
    <footer class="report-footer">
        <strong>Disclaimer:</strong> This is a personal trading tracker for monitoring your own portfolio bets. It is not stock tips, investment advice, or buy/sell recommendations.
    </footer>
</main>
<script>
(function () {{
    const menus = Array.from(document.querySelectorAll(".nav-menu"));

    menus.forEach((menu) => {{
        menu.addEventListener("toggle", () => {{
            if (!menu.open) return;
            menus.forEach((otherMenu) => {{
                if (otherMenu !== menu) otherMenu.open = false;
            }});
        }});

        menu.querySelectorAll(".nav-dropdown a").forEach((link) => {{
            link.addEventListener("click", () => {{
                menu.open = false;
            }});
        }});
    }});

    document.addEventListener("click", (event) => {{
        if (event.target.closest(".nav-menu")) return;
        menus.forEach((menu) => {{
            menu.open = false;
        }});
    }});

    document.addEventListener("keydown", (event) => {{
        if (event.key !== "Escape") return;
        menus.forEach((menu) => {{
            menu.open = false;
        }});
    }});

    const money = new Intl.NumberFormat("en-IN", {{
        style: "currency",
        currency: "INR",
        maximumFractionDigits: 2
    }});

    function compactMoney(value) {{
        const number = Number(value) || 0;
        const absValue = Math.abs(number);
        const sign = number < 0 ? "-" : "";
        if (absValue >= 1_000_000_000_000) {{
            return `${{sign}}₹${{absValue.toExponential(2)}}`;
        }}
        if (absValue >= 10_000_000) {{
            return `${{sign}}₹${{(absValue / 10_000_000).toFixed(2)}}Cr`;
        }}
        if (absValue >= 100_000) {{
            return `${{sign}}₹${{(absValue / 100_000).toFixed(2)}}L`;
        }}
        return money.format(number);
    }}

    function compactPercent(value) {{
        const number = Number(value) || 0;
        if (Math.abs(number) >= 10_000) {{
            return `${{number.toExponential(2)}}%`;
        }}
        return `${{number.toFixed(2)}}%`;
    }}

    function parseDate(value) {{
        if (!value) {{
            return null;
        }}

        const text = String(value).trim();
        const isoMatch = text.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/);
        if (isoMatch) {{
            return new Date(Number(isoMatch[1]), Number(isoMatch[2]) - 1, Number(isoMatch[3]));
        }}

        const match = text.match(/^(\\d{{1,2}})[/-](\\d{{1,2}})[/-](\\d{{4}})$/);
        if (match) {{
            return new Date(Number(match[3]), Number(match[2]) - 1, Number(match[1]));
        }}

        const parsed = new Date(text);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }}

    function formatDate(value) {{
        const date = parseDate(value);
        if (!date) {{
            return value || "";
        }}

        return date.toLocaleDateString("en-IN", {{
            day: "2-digit",
            month: "short",
            year: "numeric"
        }});
    }}

    function renderChart(period) {{
        const target = document.getElementById("portfolio-chart");
        if (!target) {{
            return;
        }}

        const allPoints = JSON.parse(target.getAttribute("data-points") || "[]");
        if (!allPoints.length) {{
            target.innerHTML = '<div class="empty">No portfolio value data available.</div>';
            return;
        }}

        const datedPoints = allPoints.map((point, index) => ({{
            ...point,
            index,
            parsedDate: parseDate(point.date)
        }}));
        const latestDate = datedPoints.reduce((latest, point) => {{
            if (!point.parsedDate) {{
                return latest;
            }}
            return !latest || point.parsedDate > latest ? point.parsedDate : latest;
        }}, null);

        const daysByPeriod = {{
            "1m": 30,
            "3m": 90,
            "6m": 180,
            "1y": 365,
            "2y": 730
        }};

        const currentPeriod = period || "all";
        let points = datedPoints;
        let periodStart = null;
        if (currentPeriod === "ytd" && latestDate) {{
            periodStart = new Date(latestDate.getFullYear(), 0, 1);
            points = datedPoints.filter((point) => point.parsedDate && point.parsedDate >= periodStart);
        }} else if (currentPeriod !== "all" && latestDate) {{
            periodStart = new Date(latestDate);
            periodStart.setDate(periodStart.getDate() - daysByPeriod[currentPeriod] + 1);
            points = datedPoints.filter((point) => point.parsedDate && point.parsedDate >= periodStart);
        }}

        if (!points.length) {{
            points = datedPoints.slice(-1);
        }}

        const width = 1120;
        const height = 280;
        const padX = 16;
        const rightPad = 72;
        const padY = 28;
        const plotWidth = width - (padX * 2);
        const plotRight = width - rightPad;
        const usableWidth = plotRight - padX;
        const plotHeight = height - (padY * 2);
        const values = points.map((point) => Number(point.realized) || 0);
        const rawMin = Math.min(...values);
        const rawMax = Math.max(...values);
        const rawRange = rawMax - rawMin;
        const padding = Math.max(rawRange * 0.16, Math.max(Math.abs(rawMax), Math.abs(rawMin)) * 0.06, 1);
        const minValue = Math.min(rawMin - padding, 0);
        const maxValue = Math.max(rawMax + padding, 0, minValue + 1);
        const valueRange = Math.max(maxValue - minValue, 1);

        function valueToY(value) {{
            return padY + plotHeight - ((value - minValue) / valueRange * plotHeight);
        }}

        const scaled = points.map((point, index) => {{
            const x = padX + ((usableWidth / Math.max(points.length - 1, 1)) * index);
            const value = Number(point.realized) || 0;
            const y = valueToY(value);
            return {{ ...point, x, y }};
        }});

        const linePoints = scaled
            .map((point) => `${{point.x.toFixed(1)}},${{point.y.toFixed(1)}}`)
            .join(" ");
        const zeroY = valueToY(0);
        const areaPoints = `${{padX}},${{zeroY.toFixed(1)}} ${{linePoints}} ${{plotRight}},${{zeroY.toFixed(1)}}`;
        const zeroLine = minValue < 0 && maxValue > 0
            ? `<line x1="${{padX}}" y1="${{zeroY.toFixed(1)}}" x2="${{plotRight}}" y2="${{zeroY.toFixed(1)}}" class="zero-axis"></line>`
            : "";

        function tooltipFor(point) {{
            const realizedChange = Number(point.realizedChange) || 0;
            if (point.eventType === "buy") {{
                const quantity = Number(point.qty || 0).toLocaleString("en-IN", {{ maximumFractionDigits: 2 }});
                if (point.isClosedBet) {{
                    const sellPrice = Number(point.sellPrice || 0);
                    const buyPrice = Number(point.buyPrice || 0);
                    const realizedPl = (sellPrice - buyPrice) * Number(point.qty || 0);
                    const holdingPeriod = point.holdingDays == null ? "-" : `${{point.holdingDays}} days`;
                    const target = point.targetPrice == null ? "" : `\nTarget: ${{money.format(point.targetPrice)}}`;
                    return `${{point.label}} · Bet #${{point.betNo}}\nBuy: ${{formatDate(point.buyDate)}} · ${{money.format(point.buyPrice)}} × ${{quantity}}\nInvested: ${{money.format(point.investedAmount)}}\nSell: ${{formatDate(point.sellDate)}} · ${{money.format(point.sellPrice)}}\nRealized P/L: ${{money.format(realizedPl)}} · Held: ${{holdingPeriod}}${{target}}`;
                }}
                return `${{point.label}} · Bet #${{point.betNo}}\nBuy: ${{formatDate(point.buyDate)}} · ${{money.format(point.buyPrice)}} × ${{quantity}}\nInvested: ${{money.format(point.investedAmount)}}`;
            }}
            if (realizedChange > 0) {{
                return `${{point.label}} · Profit ${{money.format(realizedChange)}}\nSell: ${{formatDate(point.sellDate)}} · ${{point.holdingDays}} days`;
            }}
            if (realizedChange < 0) {{
                return `${{point.label}} · Loss ${{money.format(Math.abs(realizedChange))}}\nSell: ${{formatDate(point.sellDate)}} · ${{point.holdingDays}} days`;
            }}

            return `${{point.label}} · Realized ${{money.format(0)}}\nSell: ${{formatDate(point.sellDate)}} · ${{point.holdingDays}} days`;
        }}

        function starPath(x, y, outerRadius = 6.5, innerRadius = 2.9) {{
            const points = [];
            for (let i = 0; i < 10; i += 1) {{
                const angle = (-Math.PI / 2) + (i * Math.PI / 5);
                const radius = i % 2 === 0 ? outerRadius : innerRadius;
                points.push(`${{(x + Math.cos(angle) * radius).toFixed(1)}},${{(y + Math.sin(angle) * radius).toFixed(1)}}`);
            }}
            return points.join(" ");
        }}

        function trianglePath(x, y, radius = 6.5) {{
            return [
                `${{x.toFixed(1)}},${{(y - radius).toFixed(1)}}`,
                `${{(x - radius).toFixed(1)}},${{(y + radius).toFixed(1)}}`,
                `${{(x + radius).toFixed(1)}},${{(y + radius).toFixed(1)}}`
            ].join(" ");
        }}

        const markers = scaled.map((point) => {{
            const realizedChange = Number(point.realizedChange) || 0;
            if (point.eventType === "realized" && realizedChange >= 0) {{
                return `
                    <polygon class="event-star" points="${{starPath(point.x, point.y)}}">
                        <title>${{tooltipFor(point)}}</title>
                    </polygon>
                `;
            }}
            if (realizedChange < 0) {{
                return `
                    <polygon class="event-loss" points="${{trianglePath(point.x, point.y)}}">
                        <title>${{tooltipFor(point)}}</title>
                    </polygon>
                `;
            }}
            return `
                <circle class="bet-added${{point.isClosedBet ? " bet-closed" : ""}}" cx="${{point.x.toFixed(1)}}" cy="${{point.y.toFixed(1)}}" r="3.5">
                    <title>${{tooltipFor(point)}}</title>
                </circle>
            `;
        }}).join("");
        const hasLossEvent = scaled.some((point) => (Number(point.realizedChange) || 0) < 0);
        const lastPoint = points[points.length - 1];
        const firstPoint = points[0];
        const previousPoint = points.length > 1 ? points[points.length - 2] : firstPoint;
        const totalInvestment = {total_invested:.2f};
        const realizedProfit = Number(lastPoint.realized) || 0;
        const lastBookedChange = realizedProfit - (Number(previousPoint.realized) || 0);
        const periodChange = realizedProfit - (Number(firstPoint.realized) || 0);
        const realizedReturn = totalInvestment ? (realizedProfit / totalInvestment) * 100 : 0;
        const lastChangeClass = lastBookedChange >= 0 ? "positive" : "negative";
        const periodClass = periodChange >= 0 ? "positive" : "negative";
        const periodLabel = (period || "all").toUpperCase() === "ALL" ? "All Time" : (period || "all").toUpperCase();
        const yTicks = minValue < 0 && maxValue > 0
            ? [maxValue, 0, minValue]
            : [maxValue, minValue + (valueRange / 2), minValue];
        const yLabels = yTicks.map((value) => {{
            const y = valueToY(value);
            return `<text x="${{plotRight + 10}}" y="${{y + 4}}" class="chart-label axis-right">${{compactMoney(value)}}</text>`;
        }}).join("");
        function axisDateLabel(value, activePeriod) {{
            const date = value instanceof Date ? value : parseDate(value);
            if (!date) {{
                return value || "";
            }}

            const compactPeriods = new Set(["1m", "3m"]);
            const options = compactPeriods.has(activePeriod)
                ? {{ day: "2-digit", month: "short" }}
                : {{ month: "short", year: "2-digit" }};
            return date.toLocaleDateString("en-IN", options);
        }}

        function pickAxisPoints(chartPoints, activePeriod) {{
            if (chartPoints.length <= 1) {{
                return chartPoints.map((point) => ({{
                    x: point.x,
                    label: axisDateLabel(point.date, activePeriod)
                }}));
            }}

            const minLabelGap = ["1m", "3m"].includes(activePeriod) ? 120 : 138;
            const candidates = [];
            const addCandidate = (point) => {{
                if (!point) {{
                    return;
                }}
                candidates.push({{
                    x: point.x,
                    label: axisDateLabel(point.date, activePeriod),
                    parsedDate: point.parsedDate
                }});
            }};

            addCandidate(chartPoints[0]);
            if (["1m", "3m"].includes(activePeriod)) {{
                addCandidate(chartPoints[Math.floor((chartPoints.length - 1) / 2)]);
            }} else {{
                const seenMonths = new Set();
                chartPoints.forEach((point) => {{
                    if (!point.parsedDate) {{
                        return;
                    }}
                    const monthKey = `${{point.parsedDate.getFullYear()}}-${{point.parsedDate.getMonth()}}`;
                    if (!seenMonths.has(monthKey)) {{
                        seenMonths.add(monthKey);
                        addCandidate(point);
                    }}
                }});
            }}
            addCandidate(chartPoints[chartPoints.length - 1]);

            const sorted = candidates.sort((a, b) => a.x - b.x);
            const labels = [];
            const usedLabels = new Set();
            sorted.forEach((item) => {{
                const previous = labels[labels.length - 1];
                if (!item.label || usedLabels.has(item.label)) {{
                    return;
                }}
                if (!previous || item.x - previous.x >= minLabelGap) {{
                    labels.push(item);
                    usedLabels.add(item.label);
                }}
            }});
            return labels;
        }}

        const axisPoints = pickAxisPoints(scaled, currentPeriod);
        const xLabels = axisPoints
            .map((item, index) => {{
                const cssClass = index === axisPoints.length - 1 ? "chart-label end" : "chart-label";
                return `<text x="${{item.x.toFixed(1)}}" y="${{height - 5}}" class="${{cssClass}}">${{item.label}}</text>`;
            }})
            .join("");

        target.innerHTML = `
            <div class="chart-stats">
                <div class="chart-stat">
                    <span>Realized Profit</span>
                    <strong class="${{realizedProfit >= 0 ? "positive" : "negative"}}" title="${{money.format(realizedProfit)}}">${{compactMoney(realizedProfit)}}</strong>
                </div>
                <div class="chart-stat">
                    <span>Realized Return</span>
                    <strong class="${{realizedProfit >= 0 ? "positive" : "negative"}}" title="${{realizedReturn.toFixed(2)}}%">${{compactPercent(realizedReturn)}}</strong>
                </div>
                <div class="chart-stat">
                    <span>${{periodLabel}} Booked Profit</span>
                    <strong class="${{periodClass}}" title="${{money.format(periodChange)}}">${{compactMoney(periodChange)}}</strong>
                </div>
            </div>
            <div class="chart-context">
                <span class="chart-subtitle">Booked profit over time</span>
            </div>
            <svg class="line-chart" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Portfolio return">
                <line x1="${{padX}}" y1="${{height - padY}}" x2="${{plotRight}}" y2="${{height - padY}}" class="axis"></line>
                ${{zeroLine}}
                <polygon points="${{areaPoints}}" class="chart-area"></polygon>
                <polyline points="${{linePoints}}" class="chart-line realized"></polyline>
                ${{markers}}
                ${{xLabels}}
                ${{yLabels}}
            </svg>
            <div class="chart-legend">
                <span class="legend-item"><span class="legend-dot"></span> Bet added</span>
                <span class="legend-item"><span class="legend-dot closed-buy-dot"></span> Closed buy</span>
                <span class="legend-item"><span class="legend-star">&#9733;</span> Profit realized</span>
                ${{hasLossEvent ? '<span class="legend-item"><span class="legend-loss">&#9650;</span> Loss realized</span>' : ""}}
            </div>
        `;
    }}

    document.querySelectorAll(".period-button").forEach((button) => {{
        button.addEventListener("click", () => {{
            document.querySelectorAll(".period-button").forEach((item) => item.classList.remove("active"));
            button.classList.add("active");
            renderChart(button.dataset.period);
        }});
    }});

    function parseCellValue(text) {{
        const cleaned = text.replace(/INR|₹|,/g, "").replace("%", "").trim();
        const number = Number(cleaned);
        if (!Number.isNaN(number) && cleaned !== "") {{
            return number;
        }}

        const date = parseDate(text);
        if (date) {{
            return date.getTime();
        }}

        return text.toLowerCase();
    }}

    function sortValueForCell(cell) {{
        const explicitValue = cell?.dataset?.sortValue;
        if (explicitValue !== undefined && explicitValue !== "") {{
            return parseCellValue(explicitValue);
        }}
        return parseCellValue(cell?.innerText || "");
    }}

    function setupTable(table) {{
        const tbody = table.querySelector("tbody");
        const pager = table.closest(".section").querySelector("[data-pagination]");
        const search = table.closest(".section").querySelector("[data-table-search]");
        if (!tbody || !pager) {{
            return;
        }}

        const pageSize = Number(table.dataset.pageSize || 10);
        const state = {{
            allRows: Array.from(tbody.querySelectorAll("tr")),
            rows: [],
            page: 1,
            sortColumn: null,
            sortDirection: "asc"
        }};

        function applySearchAndSort() {{
            const query = (search?.value || "").trim().toLowerCase();
            state.rows = state.allRows.filter((row) => !query || row.innerText.toLowerCase().includes(query));
            if (state.sortColumn === null) {{
                return;
            }}
            state.rows.sort((a, b) => {{
                const aValue = sortValueForCell(a.children[state.sortColumn]);
                const bValue = sortValueForCell(b.children[state.sortColumn]);
                if (aValue < bValue) return state.sortDirection === "asc" ? -1 : 1;
                if (aValue > bValue) return state.sortDirection === "asc" ? 1 : -1;
                return 0;
            }});
        }}

        function renderRows() {{
            const totalPages = Math.max(Math.ceil(state.rows.length / pageSize), 1);
            state.page = Math.min(Math.max(state.page, 1), totalPages);
            const start = (state.page - 1) * pageSize;
            const visibleRows = state.rows.slice(start, start + pageSize);

            tbody.innerHTML = "";
            visibleRows.forEach((row) => tbody.appendChild(row));

            pager.innerHTML = `
                <span>Page ${{state.page}} of ${{totalPages}} · ${{state.rows.length}} rows</span>
                <button type="button" data-page="prev" ${{state.page === 1 ? "disabled" : ""}}>Prev</button>
                <button type="button" data-page="next" ${{state.page === totalPages ? "disabled" : ""}}>Next</button>
            `;
        }}

        table.querySelectorAll("th").forEach((header, columnIndex) => {{
            header.classList.add("sortable");
            header.addEventListener("click", () => {{
                const direction = state.sortColumn === columnIndex && state.sortDirection === "asc" ? "desc" : "asc";
                state.sortColumn = columnIndex;
                state.sortDirection = direction;
                state.page = 1;

                table.querySelectorAll("th").forEach((item) => {{
                    item.classList.remove("sorted-asc", "sorted-desc");
                }});
                header.classList.add(direction === "asc" ? "sorted-asc" : "sorted-desc");

                applySearchAndSort();
                renderRows();
            }});
        }});

        search?.addEventListener("input", () => {{
            state.page = 1;
            applySearchAndSort();
            renderRows();
        }});

        pager.addEventListener("click", (event) => {{
            const button = event.target.closest("button[data-page]");
            if (!button) {{
                return;
            }}

            state.page += button.dataset.page === "next" ? 1 : -1;
            renderRows();
        }});

        applySearchAndSort();
        renderRows();
    }}

    renderChart("all");
    document.querySelectorAll(".interactive-table").forEach(setupTable);
}}());
</script>
</body>
</html>
"""

        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)

        return filename
