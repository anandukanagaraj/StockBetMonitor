import logging
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

from services.nse_service import historical_stock_move_pct_from_nse, latest_price_from_nse


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
MAPPING_PATH = BASE_DIR / "data" / "ticker_mapping.json"
YFINANCE_CACHE_DIR = BASE_DIR / "data" / "yfinance_cache"
YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_MAPPING_CACHE = None
_MAPPING_MTIME = None
_PRICE_CACHE = {}
_INDEX_HISTORY_CACHE = {}


try:
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))
except Exception:
    logger.exception("Could not set yfinance cache location: %s", YFINANCE_CACHE_DIR)


def load_mapping():
    global _MAPPING_CACHE, _MAPPING_MTIME
    if not MAPPING_PATH.exists():
        return {}
    mtime = MAPPING_PATH.stat().st_mtime
    if _MAPPING_CACHE is not None and _MAPPING_MTIME == mtime:
        return _MAPPING_CACHE
    try:
        raw_mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        _MAPPING_CACHE = {key.strip().lower(): value for key, value in raw_mapping.items()}
        _MAPPING_MTIME = mtime
        return _MAPPING_CACHE
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


def valid_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return round(price, 2)


def _fetch_latest_price(ticker):
    symbol = resolve_symbol(ticker)
    ticker_client = yf.Ticker(symbol)

    logger.info("Yahoo quote call started: ticker=%s symbol=%s", ticker, symbol)
    try:
        fast_info = ticker_client.fast_info
        for field in ("last_price", "regular_market_price", "previous_close"):
            price = valid_price(getattr(fast_info, field, None))
            if price is not None:
                logger.info(
                    "Yahoo quote price resolved: ticker=%s symbol=%s field=%s price=%s",
                    ticker,
                    symbol,
                    field,
                    price,
                )
                return price
    except Exception:
        logger.warning("Yahoo quote lookup failed: ticker=%s symbol=%s", ticker, symbol)

    logger.info("Yahoo intraday call started: ticker=%s symbol=%s period=1d interval=1m", ticker, symbol)
    try:
        intraday = ticker_client.history(period="1d", interval="1m")
        logger.debug(
            "Yahoo intraday response: ticker=%s symbol=%s rows=%s columns=%s",
            ticker,
            symbol,
            len(intraday),
            list(intraday.columns) if not intraday.empty else [],
        )
        if not intraday.empty:
            close_values = intraday["Close"].dropna()
            if not close_values.empty:
                price = valid_price(close_values.iloc[-1])
                if price is not None:
                    logger.info("Yahoo intraday price resolved: ticker=%s symbol=%s price=%s", ticker, symbol, price)
                    return price
    except Exception:
        logger.warning("Yahoo intraday lookup failed: ticker=%s symbol=%s", ticker, symbol)

    logger.info("Yahoo API call started: ticker=%s symbol=%s period=5d interval=1d", ticker, symbol)
    history = ticker_client.history(period="5d", interval="1d")
    logger.debug(
        "Yahoo API response: ticker=%s symbol=%s rows=%s columns=%s",
        ticker,
        symbol,
        len(history),
        list(history.columns) if not history.empty else [],
    )

    if not history.empty:
        close_values = history["Close"].dropna()
        if not close_values.empty:
            price = valid_price(close_values.iloc[-1])
            if price is not None:
                logger.info("Yahoo API price resolved: ticker=%s symbol=%s price=%s", ticker, symbol, price)
                return price
            logger.warning("Yahoo API returned invalid close price: ticker=%s symbol=%s", ticker, symbol)
        else:
            logger.warning("Yahoo API returned no close prices: ticker=%s symbol=%s", ticker, symbol)
    else:
        logger.warning("Yahoo API returned no rows: ticker=%s symbol=%s", ticker, symbol)

    try:
        return latest_price_from_nse(ticker)
    except Exception:
        logger.warning("NSE quote fallback failed: ticker=%s symbol=%s", ticker, symbol)
        raise ValueError(f"No price found for {symbol}")


def latest_price(ticker, max_age_seconds=300):
    cache_key = (ticker or "").strip().upper()
    now = time.time()
    cached = _PRICE_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    price = _fetch_latest_price(ticker)
    _PRICE_CACHE[cache_key] = (now + max_age_seconds, price)
    return price


def latest_prices(tickers, max_age_seconds=300):
    """Fetch multiple current prices in one Yahoo request when possible."""
    requested = {}
    for ticker in tickers:
        key = (ticker or "").strip().upper()
        if key and key not in requested:
            requested[key] = ticker

    now = time.time()
    prices = {}
    pending = {}
    for key, ticker in requested.items():
        cached = _PRICE_CACHE.get(key)
        if cached and cached[0] > now:
            prices[key] = cached[1]
        else:
            pending[key] = ticker

    if not pending:
        return prices

    symbol_to_keys = {}
    for key, ticker in pending.items():
        symbol_to_keys.setdefault(resolve_symbol(ticker), []).append(key)

    symbols = list(symbol_to_keys)
    logger.info("Yahoo bulk quote call started: symbols=%s", len(symbols))
    try:
        history = yf.download(
            tickers=symbols,
            period="1d",
            interval="1m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        for symbol, keys in symbol_to_keys.items():
            close_values = None
            if history.empty:
                continue
            if len(symbols) == 1 and "Close" in history.columns:
                close_values = history["Close"]
            elif symbol in history.columns.get_level_values(0):
                close_values = history[symbol]["Close"]
            if close_values is None:
                continue
            close_values = close_values.dropna()
            if close_values.empty:
                continue
            price = valid_price(close_values.iloc[-1])
            if price is None:
                continue
            for key in keys:
                prices[key] = price
                _PRICE_CACHE[key] = (now + max_age_seconds, price)
        logger.info("Yahoo bulk quote call completed: requested=%s resolved=%s", len(pending), sum(1 for key in pending if key in prices))
    except Exception:
        logger.exception("Yahoo bulk quote call failed; individual fallbacks will be used")

    return prices


def nifty50_history(start_date, end_date):
    """Return daily NIFTY 50 closes, cached for six hours."""
    cache_key = (str(start_date), str(end_date))
    now = time.time()
    cached = _INDEX_HISTORY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    history = yf.Ticker("^NSEI").history(start=str(start_date), end=str(end_date), interval="1d")
    points = [
        {"date": timestamp.date().isoformat(), "close": round(float(value), 2)}
        for timestamp, value in history["Close"].dropna().items()
    ] if not history.empty else []
    _INDEX_HISTORY_CACHE[cache_key] = (now + 21600, points)
    return points


def historical_stock_move_pct(ticker, buy_date, sell_date):
    symbol = resolve_symbol(ticker)
    start_date = datetime.strptime(buy_date, "%Y-%m-%d").date()
    sell_date_value = datetime.strptime(sell_date, "%Y-%m-%d").date()
    end_date = sell_date_value + timedelta(days=1)

    logger.info(
        "Yahoo historical move call started: ticker=%s symbol=%s start=%s end=%s",
        ticker,
        symbol,
        start_date,
        end_date,
    )
    history = None
    for attempt in range(1, 4):
        try:
            candidate = yf.Ticker(symbol).history(
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                interval="1d",
            )
            closes = candidate["Close"].dropna() if not candidate.empty else []
            if len(closes) >= 2:
                history = candidate
                break
            logger.warning(
                "Yahoo historical move returned insufficient rows: ticker=%s symbol=%s attempt=%s/3 rows=%s",
                ticker,
                symbol,
                attempt,
                len(closes),
            )
        except Exception:
            logger.exception(
                "Yahoo historical move attempt failed: ticker=%s symbol=%s attempt=%s/3",
                ticker,
                symbol,
                attempt,
            )
        if attempt < 3:
            time.sleep(attempt)

    if history is None:
        fallback_start = start_date - timedelta(days=7)
        fallback_end = sell_date_value + timedelta(days=8)
        logger.info(
            "Yahoo historical move wider-date fallback: ticker=%s symbol=%s start=%s end=%s",
            ticker,
            symbol,
            fallback_start,
            fallback_end,
        )
        history = yf.Ticker(symbol).history(
            start=fallback_start.isoformat(),
            end=fallback_end.isoformat(),
            interval="1d",
        )

    if history.empty:
        logger.warning(
            "Yahoo historical move unavailable, trying NSE bhavcopy fallback: ticker=%s symbol=%s",
            ticker,
            symbol,
        )
        return historical_stock_move_pct_from_nse(ticker, buy_date, sell_date)

    dated_closes = [
        (timestamp.date(), float(value))
        for timestamp, value in history["Close"].dropna().items()
        if start_date <= timestamp.date() <= sell_date_value
    ]
    if len(dated_closes) < 2:
        logger.warning(
            "Yahoo historical move has insufficient dated closes, trying NSE bhavcopy fallback: ticker=%s symbol=%s rows=%s",
            ticker,
            symbol,
            len(dated_closes),
        )
        return historical_stock_move_pct_from_nse(ticker, buy_date, sell_date)

    entry_date, entry = dated_closes[0]
    exit_date, exit_price = dated_closes[-1]
    if not entry:
        raise ValueError(f"Invalid entry price found for {symbol}")

    move_pct = (exit_price - entry) / entry * 100
    logger.info(
        "Yahoo historical move resolved: ticker=%s symbol=%s entry_date=%s entry=%s exit_date=%s exit=%s move_pct=%.2f",
        ticker,
        symbol,
        entry_date,
        entry,
        exit_date,
        exit_price,
        move_pct,
    )
    return round(move_pct, 2)
