import logging
import math

logger = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config):
        self.defaults = config["defaults"]

    def calculate_position_size(self, entry_price, stop_price, risk_amount):
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            raise ValueError("Entry and stop price cannot be the same")

        total_shares = math.floor(risk_amount / risk_per_share)
        if total_shares <= 0:
            raise ValueError(f"Risk ${risk_amount} too small for ${risk_per_share}/share risk")

        return total_shares

    def calculate_bracket_sizes(self, total_shares):
        tp1_pct = self.defaults["tp1_pct"]
        tp2_pct = self.defaults["tp2_pct"]
        runner_pct = self.defaults["runner_pct"]

        tp1_qty = math.floor(total_shares * tp1_pct)
        tp2_qty = math.floor(total_shares * tp2_pct)
        runner_qty = total_shares - tp1_qty - tp2_qty

        if tp1_qty <= 0 or tp2_qty <= 0 or runner_qty <= 0:
            raise ValueError(f"Position size {total_shares} too small to split into 3 brackets")

        return {
            "tp1": tp1_qty,
            "tp2": tp2_qty,
            "runner": runner_qty,
            "total": total_shares,
        }

    def calculate_tp_prices(self, entry_price, stop_price, side):
        risk = abs(entry_price - stop_price)

        if side == "BUY":
            tp1_price = round(entry_price + risk * 1.5, 2)
            tp2_price = round(entry_price + risk * 2.5, 2)
        else:
            tp1_price = round(entry_price - risk * 1.5, 2)
            tp2_price = round(entry_price - risk * 2.5, 2)

        return {"tp1": tp1_price, "tp2": tp2_price}

    def get_trailing_stop_pct(self):
        return self.defaults["trailing_stop_pct"]
