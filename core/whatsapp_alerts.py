from pathlib import Path
import json


STATE_FILE = Path("data/alert_state.json")


class WhatsAppAlerts:

    def __init__(self):
        if STATE_FILE.exists():
            self.state = json.loads(STATE_FILE.read_text())
        else:
            self.state = {}

    def save(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    def process(self, results):

        messages = []

        for r in results:

            bet = str(r.bet_no)
            new_status = r.status

            if new_status == "TARGET HIT":
                messages.append(
                    f"""TARGET HIT
                    Bet       : {bet}
                    Stock     : {r.ticker}
                    Qty       : {r.qty}
                    Buy Price : INR {r.buy_price:.2f}
                    Current   : INR {r.current_price:.2f}
                    Target    : INR {r.target_price:.2f}

                    P/L       : INR {r.profit:,.2f}
                    P/L %     : {r.profit_percent:.2f}%
                    Time to review for exit."""
                )

            elif new_status == "STOP LOSS HIT":
                messages.append(
                    f"""STOP LOSS HIT
                    Bet       : {bet}
                    Stock     : {r.ticker}
                    Qty       : {r.qty}
                    Buy Price : INR {r.buy_price:.2f}
                    Current   : INR {r.current_price:.2f}
                    Stop Loss : INR {r.stop_loss:.2f}
                    P/L       : INR {r.profit:,.2f}
                    P/L %     : {r.profit_percent:.2f}%"""
                )

            elif new_status == "VERY CLOSE":
                messages.append(
                    f"""VERY CLOSE TO TARGET
                    Bet       : {bet}
                    Stock     : {r.ticker}
                    Qty       : {r.qty}
                    Buy Price : INR {r.buy_price:.2f}
                    Current   : INR {r.current_price:.2f}
                    Target    : INR {r.target_price:.2f}
                    Remaining : INR {r.remaining:.2f}
                    Distance  : {r.remaining_percent:.2f}%"""
                )

            elif new_status == "NEAR TARGET":
                messages.append(
                    f"""NEAR TARGET
                    Bet       : {bet}
                    Stock     : {r.ticker}
                    Qty       : {r.qty}
                    Buy Price : INR {r.buy_price:.2f}
                    Current   : INR {r.current_price:.2f}
                    Target    : INR {r.target_price:.2f}

                    Remaining : INR {r.remaining:.2f}
                    Distance  : {r.remaining_percent:.2f}%"""
                )

            self.state[bet] = new_status

        self.save()

        return messages
