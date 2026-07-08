from dataclasses import dataclass

@dataclass
class AlertResult:
    ticker: str
    current_price: float
    target_price: float
    stop_loss: float | None
    buy_price: float
    qty: int
    profit: float
    profit_percent: float
    remaining: float
    remaining_percent: float
    status: str
    bet_no: int

class AlertEngine:

    @staticmethod
    def safe_float(x):
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def evaluate(self, position):

        current = self.safe_float(position.current_price)
        target = self.safe_float(position.target_price)
        stop = self.safe_float(position.stop_loss)

        buy = self.safe_float(position.buy_price)
        qty = position.qty or 0

        if current is None or target is None or buy is None:
            return None

        profit = (current - buy) * qty
        profit_percent = 0 if buy == 0 else ((current - buy) / buy) * 100

        remaining = target - current
        remaining_percent = 0 if target == 0 else ((remaining / target) * 100)

        if current >= target:
            status = "TARGET HIT"
        elif remaining_percent <= 2:
            status = "VERY CLOSE"
        elif remaining_percent <= 5:
            status = "NEAR TARGET"
        else:
            status = "HOLD"

        if stop is not None and current <= stop:
            status = "STOP LOSS HIT"

        return AlertResult(
            ticker=position.ticker,
            bet_no=position.bet_no,
            current_price=current,
            target_price=target,
            stop_loss=stop,
            buy_price=buy,
            qty=qty,
            profit=profit,
            profit_percent=profit_percent,
            remaining=remaining,
            remaining_percent=remaining_percent,
            status=status,
        )