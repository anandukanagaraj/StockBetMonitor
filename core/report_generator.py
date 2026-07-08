from datetime import date, datetime
from html import escape
import json
from pathlib import Path


class ReportGenerator:

    def generate_html(self, positions):

        Path("reports").mkdir(exist_ok=True)

        generated_at = datetime.now()
        filename = f"reports/report_{generated_at.strftime('%Y%m%d_%H%M%S')}.html"

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
            if value is None:
                return None

            if isinstance(value, datetime):
                return value.date()

            if isinstance(value, date):
                return value

            text_value = str(value).strip()
            if not text_value:
                return None

            for date_format in (
                "%d/%m/%Y",
                "%d-%m-%Y",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(text_value, date_format).date()
                except ValueError:
                    pass

            return None

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
            if current_price < target_price and remaining_pct <= 5:
                near_target.append(p)

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
        all_invested = sum(invested_value(p) for p in positions)
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
            closed_profit_total / all_invested * 100
            if all_invested
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
        total_wins = closed_winners_count
        win_rate = (
            total_wins / len(positions) * 100
            if positions
            else None
        )
        win_label = "win" if total_wins == 1 else "wins"
        winning_hold_days = []
        for position in closed_positions:
            if (closed_profit(position) or 0) <= 0:
                continue

            buy_date = parse_date(position.buy_date)
            sell_date = parse_date(position.sell_date)
            if buy_date is None or sell_date is None or sell_date < buy_date:
                continue

            winning_hold_days.append((sell_date - buy_date).days)

        avg_winning_hold_days = (
            sum(winning_hold_days) / len(winning_hold_days)
            if winning_hold_days
            else None
        )

        def money(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            return f"INR {value:,.2f}"

        def number(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            return f"{value:,.2f}"

        def pct(value):
            value = value_as_float(value)
            if value is None:
                return "-"
            return f"{value:+.2f}%"

        def rate(value):
            value = value_as_float(value)
            if value is None:
                return "-"
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

        def display_date(value):
            parsed_date = parse_date(value)
            if parsed_date is None:
                return text(value)
            return parsed_date.strftime("%d/%m/%Y")

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
                if remaining_pct <= 5:
                    return "Near Target", "warning"

            return "Hold", "info"

        def table_rows(rows):
            rendered = []

            for p in rows:
                row_profit = profit(p)
                row_profit_pct = profit_percent(p)
                status_label, status_class = status_for(p)

                rendered.append(
                    f"""
                    <tr>
                        <td>
                            <strong>{text(p.ticker)}</strong>
                            <span class="subtle">Bet {text(p.bet_no)}</span>
                        </td>
                        <td>{text(p.qty)}</td>
                        <td>{number(p.buy_price)}</td>
                        <td>{number(p.current_price)}</td>
                        <td><span class="pill neutral-pill">Locked</span></td>
                        <td class="{pl_class(row_profit)}">{money(row_profit)}</td>
                        <td class="{pl_class(row_profit_pct)}">{pct(row_profit_pct)}</td>
                        <td><span class="pill {status_class}">{status_label}</span></td>
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
                        <td>
                            <strong>{text(p.ticker)}</strong>
                            <span class="subtle">Bet {text(p.bet_no)}</span>
                        </td>
                        <td>{text(p.qty)}</td>
                        <td>{number(p.buy_price)}</td>
                        <td>{number(p.sell_price)}</td>
                        <td class="{pl_class(row_profit)}">{money(row_profit)}</td>
                        <td class="{pl_class(row_profit_pct)}">{pct(row_profit_pct)}</td>
                        <td>{display_date(p.buy_date)}</td>
                        <td>{display_date(p.sell_date)}</td>
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

        def chart_date(position):
            return position.sell_date or position.buy_date or ""

        def normalized_chart_date(position, previous_date=None):
            parsed_date = parse_date(chart_date(position))
            if parsed_date is None:
                return None

            today = generated_at.date()

            def swapped_date(value):
                try:
                    return date(value.year, value.day, value.month)
                except ValueError:
                    return None

            swapped = swapped_date(parsed_date)
            if swapped is None:
                return parsed_date

            if parsed_date > today and swapped <= today:
                return swapped

            if previous_date and parsed_date < previous_date and previous_date <= swapped <= today:
                return swapped

            return parsed_date

        def sort_key(position):
            bet_no = value_as_float(position.bet_no)
            if bet_no is not None:
                return bet_no
            return 0

        def portfolio_chart_data(rows):
            chart_rows = [p for p in sorted(rows, key=sort_key) if value_as_float(p.qty)]
            cumulative_invested = 0
            cumulative_realized_profit = 0
            points = []
            previous_chart_date = None

            for position in chart_rows:
                point_date = normalized_chart_date(position, previous_chart_date)
                if point_date is not None:
                    previous_chart_date = point_date

                cumulative_invested += invested_value(position)
                if position_status(position) != "open":
                    cumulative_realized_profit += closed_profit(position) or 0

                points.append(
                    {
                        "label": str(position.ticker or ""),
                        "date": point_date.strftime("%Y-%m-%d") if point_date else str(chart_date(position)),
                        "invested": round(cumulative_invested, 2),
                        "realized": round(cumulative_realized_profit, 2),
                    }
                )

            return escape(json.dumps(points), quote=True)

        def ranking_rows(rows, value_getter, value_formatter, value_class=None):
            if not rows:
                return """
                    <tr>
                        <td colspan="3" class="empty">No positions to show.</td>
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
                        <td><strong>{text(position.ticker)}</strong></td>
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

        def locked_value(_position):
            return "Locked"

        def locked_formatter(_value):
            return '<span class="pill neutral-pill">Locked</span>'

        portfolio_rows = (
            table_rows(open_positions)
            if open_positions
            else '<tr><td colspan="8" class="empty">No open positions with current prices.</td></tr>'
        )

        closed_rows = (
            closed_table_rows(closed_positions)
            if closed_positions
            else '<tr><td colspan="9" class="empty">No closed positions yet.</td></tr>'
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Monitor Report</title>
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

.page {{
    width: min(1220px, calc(100% - 32px));
    margin: 0 auto;
    padding: 32px 0 44px;
}}

.hero {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    padding: 28px;
    border: 1px solid rgba(255, 255, 255, 0.75);
    border-radius: 8px;
    background: linear-gradient(135deg, var(--brand-dark), #0f766e);
    color: #ffffff;
    box-shadow: var(--shadow);
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
}}

.hero-profit {{
    min-width: 230px;
    padding: 18px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.12);
    text-align: right;
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
    font-size: 28px;
}}

.cards {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px;
    margin: 20px 0;
}}

.card {{
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
    font-size: 27px;
    font-weight: 800;
}}

.card-note {{
    display: block;
    margin-top: 6px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
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

.table-wrap {{
    overflow-x: auto;
}}

table {{
    width: 100%;
    border-collapse: collapse;
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
    white-space: nowrap;
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
    gap: 24px;
    margin-top: 26px;
}}

.chart-stat {{
    min-width: 150px;
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
}}

.axis {{
    stroke: #94a3b8;
    stroke-width: 1.2;
}}

.chart-area {{
    fill: rgba(14, 165, 233, 0.12);
}}

.chart-line {{
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 3;
}}

.chart-line.realized {{
    stroke: #0284c7;
}}

.chart-area {{
    fill: rgba(14, 165, 233, 0.10);
}}

.line-chart circle {{
    fill: #ffffff;
    stroke: #0284c7;
    stroke-width: 3;
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
        flex-direction: column;
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
}}

@media (max-width: 640px) {{
    .page {{
        width: min(100% - 20px, 1220px);
        padding-top: 10px;
    }}

    h1 {{
        font-size: 27px;
    }}

    .cards,
    .grid {{
        grid-template-columns: 1fr;
    }}
}}
</style>
</head>
<body>
<main class="page">
    <section class="hero">
        <div>
            <p class="eyebrow">Daily Portfolio Report</p>
            <h1>Stock Monitor</h1>
            <p class="hero-meta">Generated {generated_at.strftime("%d-%b-%Y at %I:%M %p")}</p>
        </div>
        <div class="hero-metrics">
            <div class="hero-profit">
                <span>Total investment</span>
                <strong>{money(all_invested)}</strong>
                <span>Open + closed trade capital</span>
            </div>
            <div class="hero-profit">
                <span>Realized profit</span>
                <strong class="{pl_class(closed_profit_total)}">{money(closed_profit_total)}</strong>
                <span>{rate(realized_return_pct)} realized return</span>
            </div>
        </div>
    </section>

    <section class="cards" aria-label="Summary">
        <article class="card">
            <h2>Total Positions</h2>
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
            <h2>Win Rate</h2>
            <p class="{pl_class(win_rate)}">{rate(win_rate) if win_rate is not None else "-"}</p>
            <span class="card-note">{total_wins} {win_label} / {len(positions)} trades</span>
        </article>
        <article class="card">
            <h2>Closed P/L</h2>
            <p class="{pl_class(closed_profit_total)}">{money(closed_profit_total)}</p>
        </article>
        <article class="card">
            <h2>Avg Winning Hold</h2>
            <p>{days(avg_winning_hold_days)}</p>
            <span class="card-note">Based on {len(winning_hold_days)} {win_label}</span>
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
                        <tr><th>Rank</th><th>Ticker</th><th>Access</th></tr>
                    </thead>
                    <tbody>
                        {ranking_rows(near_target, locked_value, locked_formatter)}
                    </tbody>
                </table>
            </div>
        </article>
    </section>

    <section class="section">
        <h2>Open Positions</h2>
        <div class="table-wrap">
            <table class="interactive-table" data-page-size="10">
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Qty</th>
                        <th>Buy</th>
                        <th>Current</th>
                        <th>Target</th>
                        <th>P/L</th>
                        <th>P/L %</th>
                        <th>Status</th>
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
        <h2>Closed Positions</h2>
        <div class="table-wrap">
            <table class="interactive-table" data-page-size="10">
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Qty</th>
                        <th>Buy</th>
                        <th>Sell</th>
                        <th>Realized P/L</th>
                        <th>P/L %</th>
                        <th>Buy Date</th>
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
</main>
<script>
(function () {{
    const money = new Intl.NumberFormat("en-IN", {{
        style: "currency",
        currency: "INR",
        maximumFractionDigits: 2
    }});

    function parseDate(value) {{
        if (!value) {{
            return null;
        }}

        const text = String(value).trim();
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

        let points = datedPoints;
        if (period === "ytd" && latestDate) {{
            const start = new Date(latestDate.getFullYear(), 0, 1);
            points = datedPoints.filter((point) => point.parsedDate && point.parsedDate >= start);
        }} else if (period !== "all" && latestDate) {{
            const start = new Date(latestDate);
            start.setDate(start.getDate() - daysByPeriod[period] + 1);
            points = datedPoints.filter((point) => point.parsedDate && point.parsedDate >= start);
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
        const padding = Math.max((rawMax - rawMin) * 0.18, rawMax * 0.04, 1);
        const minValue = rawMin - padding;
        const maxValue = rawMax + padding;
        const valueRange = Math.max(maxValue - minValue, 1);

        const scaled = points.map((point, index) => {{
            const x = padX + ((usableWidth / Math.max(points.length - 1, 1)) * index);
            const value = Number(point.realized) || 0;
            const y = padY + plotHeight - ((value - minValue) / valueRange * plotHeight);
            return {{ ...point, x, y }};
        }});

        const linePoints = scaled
            .map((point) => `${{point.x.toFixed(1)}},${{point.y.toFixed(1)}}`)
            .join(" ");
        const areaPoints = `${{padX}},${{height - padY}} ${{linePoints}} ${{plotRight}},${{height - padY}}`;
        const markers = scaled.map((point) => `
            <circle cx="${{point.x.toFixed(1)}}" cy="${{point.y.toFixed(1)}}" r="3.5">
                <title>${{point.label}} - realized profit ${{money.format(point.realized)}}</title>
            </circle>
        `).join("");
        const lastPoint = points[points.length - 1];
        const firstPoint = points[0];
        const previousPoint = points.length > 1 ? points[points.length - 2] : firstPoint;
        const totalInvestment = Number(lastPoint.invested) || 0;
        const realizedProfit = Number(lastPoint.realized) || 0;
        const lastBookedChange = realizedProfit - (Number(previousPoint.realized) || 0);
        const periodChange = realizedProfit - (Number(firstPoint.realized) || 0);
        const realizedReturn = totalInvestment ? (realizedProfit / totalInvestment) * 100 : 0;
        const lastChangeClass = lastBookedChange >= 0 ? "positive" : "negative";
        const periodClass = periodChange >= 0 ? "positive" : "negative";
        const periodLabel = (period || "all").toUpperCase() === "ALL" ? "All Time" : (period || "all").toUpperCase();
        const yTicks = [maxValue, minValue + (valueRange / 2), minValue];
        const yLabels = yTicks.map((value) => {{
            const y = padY + plotHeight - ((value - minValue) / valueRange * plotHeight);
            return `<text x="${{plotRight + 10}}" y="${{y + 4}}" class="chart-label axis-right">${{money.format(value)}}</text>`;
        }}).join("");
        const xLabels = scaled
            .filter((_, index) => index === 0 || index === Math.floor((scaled.length - 1) / 2) || index === scaled.length - 1)
            .map((point, index, labels) => {{
                const cssClass = index === labels.length - 1 ? "chart-label end" : "chart-label";
                return `<text x="${{point.x.toFixed(1)}}" y="${{height - 5}}" class="${{cssClass}}">${{formatDate(point.date)}}</text>`;
            }})
            .join("");

        target.innerHTML = `
            <div class="chart-stats">
                <div class="chart-stat">
                    <span>Realized Profit</span>
                    <strong class="${{realizedProfit >= 0 ? "positive" : "negative"}}">${{money.format(realizedProfit)}}</strong>
                </div>
                <div class="chart-stat">
                    <span>Realized Return</span>
                    <strong class="${{realizedProfit >= 0 ? "positive" : "negative"}}">${{realizedReturn.toFixed(2)}}%</strong>
                </div>
                <div class="chart-stat">
                    <span>${{periodLabel}} Booked Change</span>
                    <strong class="${{periodClass}}">${{money.format(periodChange)}}</strong>
                </div>
            </div>
            <svg class="line-chart" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Portfolio return">
                <line x1="${{padX}}" y1="${{height - padY}}" x2="${{plotRight}}" y2="${{height - padY}}" class="axis"></line>
                <polygon points="${{areaPoints}}" class="chart-area"></polygon>
                <polyline points="${{linePoints}}" class="chart-line realized"></polyline>
                ${{markers}}
                ${{xLabels}}
                ${{yLabels}}
            </svg>
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
        const cleaned = text.replace(/INR|,/g, "").replace("%", "").trim();
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

    function setupTable(table) {{
        const tbody = table.querySelector("tbody");
        const pager = table.closest(".section").querySelector("[data-pagination]");
        if (!tbody || !pager) {{
            return;
        }}

        const pageSize = Number(table.dataset.pageSize || 10);
        const state = {{
            rows: Array.from(tbody.querySelectorAll("tr")),
            page: 1,
            sortColumn: null,
            sortDirection: "asc"
        }};

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

                state.rows.sort((a, b) => {{
                    const aValue = parseCellValue(a.children[columnIndex]?.innerText || "");
                    const bValue = parseCellValue(b.children[columnIndex]?.innerText || "");

                    if (aValue < bValue) {{
                        return direction === "asc" ? -1 : 1;
                    }}
                    if (aValue > bValue) {{
                        return direction === "asc" ? 1 : -1;
                    }}
                    return 0;
                }});

                renderRows();
            }});
        }});

        pager.addEventListener("click", (event) => {{
            const button = event.target.closest("button[data-page]");
            if (!button) {{
                return;
            }}

            state.page += button.dataset.page === "next" ? 1 : -1;
            renderRows();
        }});

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
