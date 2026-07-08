import yfinance as yf
from typing import Dict, List
import json
from pathlib import Path


class YahooService:

    def __init__(self, mapping_file: str = "data/ticker_mapping.json"):

        self.mapping_path = Path(mapping_file)

        if self.mapping_path.exists():
            with open(self.mapping_path, "r", encoding="utf-8") as f:
                self.mapping = json.load(f)
        else:
            self.mapping = {}

    # -----------------------------
    # Resolve Yahoo symbol
    # -----------------------------
    def resolve_symbol(self, ticker: str) -> str:

        ticker = ticker.strip()

        # 1. Manual mapping (highest priority)
        if ticker in self.mapping:
            return self.mapping[ticker]

        # 2. Already valid Yahoo format
        if ".NS" in ticker or ".BO" in ticker:
            return ticker

        # 3. Default NSE format
        return f"{ticker}.NS"

    # -----------------------------
    # Safe last price extractor
    # -----------------------------
    def _safe_last_price(self, series):

        try:
            series = series.dropna()

            if len(series) == 0:
                return None

            value = series.iloc[-1]

            if value is None:
                return None

            if str(value).lower() == "nan":
                return None

            return float(value)

        except Exception:
            return None

    def _fallback_price(self, symbol: str):

        try:
            data = yf.Ticker(symbol).history(
                period="5d",
                interval="1d",
            )

            if data.empty or "Close" not in data:
                return None

            return self._safe_last_price(data["Close"])

        except Exception:
            return None

    # -----------------------------
    # Fetch all prices
    # -----------------------------
    def fetch_prices(self, tickers: List[str]) -> Dict[str, float]:

        if not tickers:
            return {}

        symbol_map = {}
        reverse_map = {}

        for t in tickers:
            symbol = self.resolve_symbol(t)
            symbol_map[t] = symbol
            reverse_map[symbol] = t

        yf_symbols = list(set(symbol_map.values()))  # remove duplicates

        try:
            data = yf.download(
                tickers=" ".join(yf_symbols),
                period="1d",
                interval="1m",
                progress=False,
                group_by="ticker",
                threads=True
            )

        except Exception:
            prices = {}
            for original_ticker, symbol in symbol_map.items():
                price = self._fallback_price(symbol)
                if price is not None:
                    prices[original_ticker] = price
            return prices

        prices = {}

        # -----------------------------
        # Single ticker case
        # -----------------------------
        if len(yf_symbols) == 1:

            symbol = yf_symbols[0]

            try:
                price = self._safe_last_price(data["Close"])

                if price is None:
                    price = self._fallback_price(symbol)

                if price is not None:
                    prices[tickers[0]] = price

            except Exception:
                price = self._fallback_price(symbol)
                if price is not None:
                    prices[tickers[0]] = price

            return prices

        # -----------------------------
        # Multiple tickers case
        # -----------------------------
        for symbol in yf_symbols:

            try:

                price = None

                if symbol in data:
                    series = data[symbol]["Close"]
                    price = self._safe_last_price(series)

                if price is None:
                    price = self._fallback_price(symbol)

                original_ticker = reverse_map.get(symbol)

                if original_ticker:
                    prices[original_ticker] = price

            except Exception:
                continue

        return prices
