import yfinance as yf

ticker = yf.Ticker("TANLA.NS")

info = ticker.fast_info

print(info["lastPrice"])