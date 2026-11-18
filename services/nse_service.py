import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests


logger = logging.getLogger("app")
BASE_DIR = Path(__file__).resolve().parents[1]
BHAVCOPY_CACHE_DIR = BASE_DIR / "data" / "nse_bhavcopy_cache"
BHAVCOPY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/csv,application/zip,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


def normalize_nse_symbol(ticker):
    return (ticker or "").strip().upper().replace(".NS", "")


def bhavcopy_urls(day):
    month = day.strftime("%b").upper()
    return [
        (
            "udiff",
            f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{day.strftime('%Y%m%d')}_F_0000.csv.zip",
        ),
        (
            "full",
            f"https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{day.strftime('%d%m%Y')}.csv",
        ),
        (
            "legacy",
            f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{day.year}/{month}/cm{day.strftime('%d%b%Y').upper()}bhav.csv.zip",
        ),
    ]


def read_cached_bhavcopy(day):
    cache_path = BHAVCOPY_CACHE_DIR / f"nse_cm_{day.strftime('%Y%m%d')}.csv"
    if cache_path.exists():
        logger.debug("NSE bhavcopy cache hit: date=%s path=%s", day, cache_path)
        return cache_path.read_text(encoding="utf-8", errors="replace")
    return None


def write_cached_bhavcopy(day, content):
    cache_path = BHAVCOPY_CACHE_DIR / f"nse_cm_{day.strftime('%Y%m%d')}.csv"
    cache_path.write_text(content, encoding="utf-8")


def csv_from_response(response):
    content_type = response.headers.get("content-type", "").lower()
    content = response.content
    if "zip" in content_type or content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("NSE zip did not contain a CSV file")
            with archive.open(csv_names[0]) as handle:
                return handle.read().decode("utf-8", errors="replace")
    return content.decode("utf-8", errors="replace")


def fetch_bhavcopy_csv(day):
    cached = read_cached_bhavcopy(day)
    if cached:
        return cached

    session = requests.Session()
    for source, url in bhavcopy_urls(day):
        try:
            logger.info("NSE bhavcopy fetch started: source=%s date=%s url=%s", source, day, url)
            response = session.get(url, headers=NSE_HEADERS, timeout=20)
            response.raise_for_status()
            csv_text = csv_from_response(response)
            if "SYMBOL" not in csv_text.upper() and "TCKRSYMB" not in csv_text.upper():
                raise ValueError("NSE bhavcopy response does not look like equity CSV data")
            write_cached_bhavcopy(day, csv_text)
            logger.info("NSE bhavcopy fetch succeeded: source=%s date=%s", source, day)
            return csv_text
        except Exception:
            logger.exception("NSE bhavcopy fetch failed: source=%s date=%s", source, day)

    raise ValueError(f"No NSE bhavcopy found for {day}")


def row_value(row, names):
    normalized = {key.strip().upper(): value for key, value in row.items() if key}
    for name in names:
        value = normalized.get(name.upper())
        if value not in (None, ""):
            return value
    return None


def close_price_from_bhavcopy(ticker, day):
    symbol = normalize_nse_symbol(ticker)
    csv_text = fetch_bhavcopy_csv(day)
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row_symbol = (row_value(row, ["SYMBOL", "TckrSymb", "TCKRSYMB"]) or "").strip().upper()
        series = (row_value(row, ["SERIES", "SctySrs", "SCTYSRS"]) or "EQ").strip().upper()
        if row_symbol != symbol or series not in {"EQ", ""}:
            continue
        close_text = row_value(row, ["CLOSE_PRICE", "CLOSE", "ClsPric", "CLSPRIC", "CLOSE_PRICE"])
        if close_text is None:
            continue
        close_price = float(str(close_text).replace(",", ""))
        logger.info("NSE bhavcopy close resolved: ticker=%s date=%s close=%s", ticker, day, close_price)
        return close_price
    raise ValueError(f"{symbol} close price not found in NSE bhavcopy for {day}")


def nearest_close_price(ticker, target_date, max_lookback_days=7):
    for offset in range(0, max_lookback_days + 1):
        day = target_date - timedelta(days=offset)
        try:
            close_price = close_price_from_bhavcopy(ticker, day)
            return day, close_price
        except Exception:
            logger.debug("NSE bhavcopy close unavailable: ticker=%s date=%s", ticker, day)
    raise ValueError(f"No NSE close price found for {ticker} near {target_date}")


def latest_price_from_nse(ticker):
    symbol = normalize_nse_symbol(ticker)
    session = requests.Session()
    headers = {
        **NSE_HEADERS,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/get-quotes/equity",
    }
    logger.info("NSE quote call started: ticker=%s symbol=%s", ticker, symbol)
    session.get("https://www.nseindia.com", headers=headers, timeout=10)
    response = session.get(
        "https://www.nseindia.com/api/quote-equity",
        params={"symbol": symbol},
        headers=headers,
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    price_info = payload.get("priceInfo") or {}
    for field in ("lastPrice", "close", "previousClose"):
        value = price_info.get(field)
        try:
            price = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if price > 0:
            logger.info("NSE quote price resolved: ticker=%s symbol=%s field=%s price=%s", ticker, symbol, field, price)
            return round(price, 2)
    raise ValueError(f"No NSE quote price found for {symbol}")


def historical_stock_move_pct_from_nse(ticker, buy_date, sell_date):
    buy_day = datetime.strptime(buy_date, "%Y-%m-%d").date()
    sell_day = datetime.strptime(sell_date, "%Y-%m-%d").date()
    entry_date, entry = nearest_close_price(ticker, buy_day)
    exit_date, exit_price = nearest_close_price(ticker, sell_day)
    if not entry:
        raise ValueError(f"Invalid NSE entry price found for {ticker}")
    move_pct = (exit_price - entry) / entry * 100
    logger.info(
        "NSE historical move resolved: ticker=%s entry_date=%s entry=%s exit_date=%s exit=%s move_pct=%.2f",
        ticker,
        entry_date,
        entry,
        exit_date,
        exit_price,
        move_pct,
    )
    return round(move_pct, 2)
