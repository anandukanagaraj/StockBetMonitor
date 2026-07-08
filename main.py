from core.logger import setup_logger
from core.config import Config
from core.excel_reader import ExcelReader
from core.yahoo_service import YahooService
from core.excel_writer import ExcelWriter
from core.alert_engine import AlertEngine
from core.report_generator import ReportGenerator
from core.whatsapp_service import WhatsAppService
from core.whatsapp_alerts import WhatsAppAlerts

logger = setup_logger()

logger.info("=" * 40)
logger.info("Stock Monitor Starting...")
logger.info("=" * 40)

try:

    # -------------------------
    # Load Configuration
    # -------------------------

    config = Config()

    logger.info(f"Excel File   : {config.excel_file}")
    logger.info(f"Market Opens : {config.market_start}")
    logger.info(f"Market Closes: {config.market_end}")
    logger.info(f"Report Time  : {config.report_time}")

    # -------------------------
    # Load Portfolio
    # -------------------------

    reader = ExcelReader(config.excel_file)
    positions = reader.load_positions()

    logger.info("")
    logger.info(f"Open Positions : {len(positions)}")

    # -------------------------
    # Get Live Prices
    # -------------------------

    service = YahooService()

    tickers = [p.ticker for p in positions]

    prices = service.fetch_prices(tickers)

    # -------------------------
    # Process Positions
    # -------------------------

    engine = AlertEngine()
    whatsapp = WhatsAppService()

    results = []

    logger.info("")
    logger.info("=" * 110)
    logger.info(
        f"{'Ticker':15}"
        f"{'Current':>10}"
        f"{'Target':>10}"
        f"{'P/L ₹':>12}"
        f"{'P/L %':>10}"
        f"{'Remain':>12}"
        f"{'Status':>18}"
    )
    logger.info("=" * 110)

    for p in positions:

        current = prices.get(p.ticker)

        if current is None:
            logger.warning(f"{p.ticker} : Live price unavailable")
            continue

        p.current_price = current

        result = engine.evaluate(p)

        if result is None:
            continue

        logger.info(f"{result.ticker} | Bet={result.bet_no} | Status={result.status}")

        whatsapp.process(result)
        results.append(result)

        logger.info(
            f"{result.ticker[:15]:15}"
            f"{result.current_price:10.2f}"
            f"{result.target_price:10.2f}"
            f"{result.profit:12.2f}"
            f"{result.profit_percent:10.2f}"
            f"{result.remaining:12.2f}"
            f"{result.status:>18}"
        )

    # -------------------------
    # Update Excel
    # -------------------------

    writer = ExcelWriter(config.excel_file)

    updated = writer.update_prices(positions)

    logger.info("")
    logger.info(f"Updated {updated} rows in Excel.")

    # -------------------------
    # End Of Day Report
    # -------------------------

    report = ReportGenerator()

    wa = WhatsAppAlerts()
    alerts = wa.process(results)

    for msg in alerts:
        print()
        print("=" * 60)
        print(msg)

    logger.info("")
    report_positions = reader.load_positions(open_only=False)
    for report_position in report_positions:
        matching_position = next(
            (p for p in positions if p.bet_no == report_position.bet_no),
            None,
        )
        if matching_position is not None:
            report_position.current_price = matching_position.current_price

    report_file = report.generate_html(report_positions)

    logger.info("")
    logger.info(f"HTML report generated: {report_file}")

except Exception as ex:
    logger.exception(ex)
