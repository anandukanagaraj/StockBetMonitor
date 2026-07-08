from dataclasses import dataclass
from typing import Optional


@dataclass
class Position:
    bet_no: int
    buy_date: Optional[str]
    sell_date: Optional[str]

    ticker: str
    qty: int

    buy_price: float
    trade_capital: float

    target_price: float
    stop_loss: Optional[float]

    current_price: Optional[float]

    sell_price: Optional[float]

    target_profit: Optional[float]
    target_loss: Optional[float]

    achieved_pl: Optional[float]

    status: str