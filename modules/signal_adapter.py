import logging

logger = logging.getLogger(__name__)


class SignalAdapter:
    """Thin abstraction for trade signal input.
    Phase 1: accepts manual input from UI.
    Phase 2: will accept webhook/API signals from scanner/TradingView.
    """

    def __init__(self, execution_engine):
        self.engine = execution_engine

    def process_manual_signal(self, symbol, side, entry_price, stop_price, risk_amount):
        return self.engine.execute(symbol, side, entry_price, stop_price, risk_amount)

    def process_webhook_signal(self, payload):
        """Phase 2: Process incoming webhook signal.
        Expected payload format:
        {
            "symbol": "AAPL",
            "side": "BUY",
            "entry_price": 150.00,
            "stop_price": 148.00,
            "risk_amount": 100.00
        }
        """
        required = ["symbol", "side", "entry_price", "stop_price", "risk_amount"]
        for field in required:
            if field not in payload:
                raise ValueError(f"Missing required field: {field}")

        return self.engine.execute(
            payload["symbol"],
            payload["side"],
            float(payload["entry_price"]),
            float(payload["stop_price"]),
            float(payload["risk_amount"]),
        )
