import logging
import json
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
MAPPING_PATH = BASE_DIR / "data" / "ticker_mapping.json"
YFINANCE_CACHE_DIR = BASE_DIR / "data" / "yfinance_cache"
YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

try:
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
except Exception:
    logger.exception("Could not set yfinance cache location: %s", YFINANCE_CACHE_DIR)


def load_mapping():
    if not MAPPING_PATH.exists():
        return {}
    try:
        raw_mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        return {key.strip().lower(): value for key, value in raw_mapping.items()}
    except Exception:
        logger.exception("Could not load ticker mapping: %s", MAPPING_PATH)
        return {}


def resolve_symbol(ticker):
    text = ticker.strip()
    mapping = load_mapping()
    mapped = mapping.get(text.lower())
    if mapped:
        logger.debug("Ticker mapping resolved: ticker=%s symbol=%s", ticker, mapped)
        return mapped

    symbol = text.upper()
    if not symbol.endswith(".NS") and "." not in symbol:
        symbol = f"{symbol}.NS"
    return symbol


def latest_price(ticker):
    symbol = resolve_symbol(ticker)

    logger.info("Yahoo API call started: ticker=%s symbol=%s period=5d interval=1d", ticker, symbol)
    history = yf.Ticker(symbol).history(period="5d", interval="1d")
    logger.debug(
        "Yahoo API response: ticker=%s symbol=%s rows=%s columns=%s",
        ticker,
        symbol,
        len(history),
        list(history.columns) if not history.empty else [],
    )

    if history.empty:
        logger.warning("Yahoo API returned no rows: ticker=%s symbol=%s", ticker, symbol)
        raise ValueError(f"No price found for {symbol}")

    close_values = history["Close"].dropna()
    if close_values.empty:
        logger.warning("Yahoo API returned no close prices: ticker=%s symbol=%s", ticker, symbol)
        raise ValueError(f"No close price found for {symbol}")

    price = round(float(close_values.iloc[-1]), 2)
    logger.info("Yahoo API price resolved: ticker=%s symbol=%s price=%s", ticker, symbol, price)
    return price


def historical_stock_move_pct(ticker, buy_date, sell_date):
    symbol = resolve_symbol(ticker)
    start_date = datetime.strptime(buy_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(sell_date, "%Y-%m-%d").date() + timedelta(days=1)

    logger.info(
        "Yahoo historical move call started: ticker=%s symbol=%s start=%s end=%s",
        ticker,
        symbol,
        start_date,
        end_date,
    )
    history = yf.Ticker(symbol).history(start=start_date.isoformat(), end=end_date.isoformat(), interval="1d")
    if history.empty:
        raise ValueError(f"No historical prices found for {symbol}")

    closes = history["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"Not enough historical prices found for {symbol}")

    entry = float(closes.iloc[0])
    exit_price = float(closes.iloc[-1])
    if not entry:
        raise ValueError(f"Invalid entry price found for {symbol}")

    move_pct = (exit_price - entry) / entry * 100
    logger.info(
        "Yahoo historical move resolved: ticker=%s symbol=%s entry=%s exit=%s move_pct=%.2f",
        ticker,
        symbol,
        entry,
        exit_price,
        move_pct,
    )
    return round(move_pct, 2)
