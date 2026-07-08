from core.alert_history import AlertHistory


class WhatsAppService:

    def __init__(self):
        self.history = AlertHistory()

    def process(self, result):

        if result.status not in (
            "TARGET HIT",
            "STOP LOSS HIT",
            "VERY CLOSE",
        ):
            return

        self.send(result)
        self.history.set(f"{result.ticker}-{result.bet_no}", result.status)

    def send(self, result):

        print()

        print("=" * 60)
        print("WHATSAPP ALERT")
        print("=" * 60)

        print(f"Stock   : {result.ticker}")
        print(f"Bet     : #{result.bet_no}")

        print(f"Current : INR {result.current_price:.2f}")
        print(f"Target  : INR {result.target_price:.2f}")

        if result.stop_loss:
            print(f"Stop    : INR {result.stop_loss:.2f}")

        print(f"Status  : {result.status}")

        print(f"P/L     : INR {result.profit:.2f}")
        print(f"P/L %   : {result.profit_percent:.2f}%")

        print("=" * 60)
