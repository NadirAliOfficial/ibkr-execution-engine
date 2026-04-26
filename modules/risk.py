import logging
import math

logger = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config):
        self.defaults = config["defaults"]
        self._modes = config.get("modes", {})

    def calculate_position_size(self, entry_price, stop_price, risk_amount):
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            raise ValueError("Entry and stop price cannot be the same")

        total_shares = math.floor(risk_amount / risk_per_share)
        if total_shares <= 0:
            raise ValueError(f"Risk ${risk_amount} too small for ${risk_per_share}/share risk")

        return total_shares

    def calculate_bracket_sizes(self, total_shares, mode_cfg=None):
        if mode_cfg is None:
            # backward compat: use defaults if present
            tp1_pct = self.defaults.get("tp1_pct", 0.50)
            tp2_pct = self.defaults.get("tp2_pct", 0.40)
            runner_pct = self.defaults.get("runner_pct", 0.10)
        else:
            tp1_pct = mode_cfg["tp1_pct"]
            tp2_pct = mode_cfg["tp2_pct"]
            runner_pct = mode_cfg["runner_pct"]

        if abs(tp1_pct + tp2_pct + runner_pct - 1.0) > 0.001:
            raise ValueError("Mode percentages must sum to 100%")

        tp1_qty = math.floor(total_shares * tp1_pct)
        tp2_qty = math.floor(total_shares * tp2_pct)
        runner_qty = math.floor(total_shares * runner_pct)
        # Assign rounding residual to tp2 — never to runner (keeps scalp runner=0)
        tp2_qty += total_shares - tp1_qty - tp2_qty - runner_qty

        if tp1_qty <= 0 or tp2_qty <= 0:
            raise ValueError(f"Position size {total_shares} too small for this mode's percentages")

        if runner_pct > 0 and runner_qty <= 0:
            raise ValueError(f"Position size {total_shares} too small for runner allocation in this mode")

        return {
            "tp1": tp1_qty,
            "tp2": tp2_qty,
            "runner": runner_qty,
            "total": total_shares,
        }

    def validate_mode(self, modes_config):
        for name, cfg in modes_config.items():
            total = cfg.get("tp1_pct", 0) + cfg.get("tp2_pct", 0) + cfg.get("runner_pct", 0)
            if abs(total - 1.0) > 0.001:
                raise ValueError(f"Mode '{name}' percentages sum to {total:.2f}, must be 1.0")

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
