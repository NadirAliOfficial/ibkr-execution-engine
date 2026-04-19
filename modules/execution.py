import logging

logger = logging.getLogger(__name__)


class ExecutionEngine:

    def __init__(self, broker, risk_manager, order_manager):
        self.broker = broker
        self.risk = risk_manager
        self.orders = order_manager

        self.broker.set_fill_callback(self._on_fill)
        self.broker.set_status_callback(self._on_status)
        self.broker.set_disconnect_callback(self._on_disconnect)

    def execute(self, symbol, side, entry_price, stop_price, risk_amount,
                mode=None, session_mode=None):
        if not self.broker.is_connected:
            raise ConnectionError("Not connected to IBKR Gateway")

        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Side must be BUY or SELL")

        if side == "BUY" and stop_price >= entry_price:
            raise ValueError("Stop price must be below entry price for BUY")
        if side == "SELL" and stop_price <= entry_price:
            raise ValueError("Stop price must be above entry price for SELL")

        if risk_amount <= 0:
            raise ValueError("Risk amount must be positive")

        trade = self.orders.create_trade(symbol, side, entry_price, stop_price, risk_amount,
                                         mode=mode, session_mode=session_mode)
        trade = self.orders.execute_trade(trade["trade_id"])

        logger.info(f"Execution started: {trade['trade_id']}")
        return trade

    def _on_fill(self, ib_trade, fill):
        order_id = ib_trade.order.orderId
        avg_price = fill.execution.avgPrice
        cumulative = fill.execution.cumQty
        self.orders.handle_fill(order_id, cumulative, avg_price)

    def _on_status(self, ib_trade):
        order_id = ib_trade.order.orderId
        status = ib_trade.orderStatus.status
        self.orders.handle_order_status(order_id, status)

    def _on_disconnect(self):
        logger.warning("Connection lost — attempting reconnect")
        if self.broker.reconnect():
            logger.info("Reconnected — recovering trade state")
            self.orders.recover_state()
        else:
            logger.error("Reconnect failed — manual intervention required")

    def cancel_trade(self, trade_id):
        from datetime import datetime
        from modules.order_manager import TradeState
        trade = self.orders.trades.get(trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")
        # Cancel IB orders via queued broker calls (safe from Flask thread)
        all_order_ids = set(trade["order_ids"].values())
        try:
            open_trades = self.broker.get_open_trades()
            if open_trades:
                for t in open_trades:
                    if t.order.orderId in all_order_ids:
                        try:
                            self.broker.cancel_order(t)
                        except Exception as e:
                            logger.warning(f"Could not cancel order {t.order.orderId}: {e}")
        except Exception as e:
            logger.warning(f"Error cancelling IB orders for {trade_id}: {e}")
        trade["state"] = TradeState.CANCELLED
        trade["updated_at"] = datetime.now().isoformat()
        self.orders._save_state()
        logger.info(f"Trade {trade_id} cancelled")

    def get_status(self, trade_id=None):
        if trade_id:
            return self.orders.get_trade_status(trade_id)
        return self.orders.get_all_trades()

    def get_active_trades(self):
        return self.orders.get_active_trades()
