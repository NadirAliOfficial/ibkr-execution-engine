import logging
import time
from ib_insync import IB, Stock, LimitOrder, StopOrder, Order, Trade, util

logger = logging.getLogger(__name__)


class IBKRBroker:

    def __init__(self, config):
        self.config = config["ibkr"]
        self.ib = IB()
        self._connected = False
        self._on_fill_callback = None
        self._on_status_callback = None
        self._on_disconnect_callback = None

    def connect(self):
        try:
            self.ib.connect(
                self.config["host"],
                self.config["port"],
                clientId=self.config["client_id"],
                timeout=self.config["timeout"],
                readonly=self.config["readonly"],
            )
            self._connected = True
            self.ib.disconnectedEvent += self._handle_disconnect
            self.ib.execDetailsEvent += self._handle_exec_details
            self.ib.orderStatusEvent += self._handle_order_status
            logger.info("Connected to IBKR Gateway")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")

    def reconnect(self, max_retries=5, delay=5):
        for attempt in range(1, max_retries + 1):
            logger.info(f"Reconnect attempt {attempt}/{max_retries}")
            if self.connect():
                return True
            time.sleep(delay)
        logger.error("Failed to reconnect after all attempts")
        return False

    @property
    def is_connected(self):
        return self._connected and self.ib.isConnected()

    def set_fill_callback(self, callback):
        self._on_fill_callback = callback

    def set_status_callback(self, callback):
        self._on_status_callback = callback

    def set_disconnect_callback(self, callback):
        self._on_disconnect_callback = callback

    def _handle_disconnect(self):
        self._connected = False
        logger.warning("Disconnected from IBKR Gateway")
        if self._on_disconnect_callback:
            self._on_disconnect_callback()

    def _handle_exec_details(self, trade, fill):
        logger.info(f"Fill: {trade.contract.symbol} {fill.execution.side} "
                     f"{fill.execution.shares}@{fill.execution.price} "
                     f"orderId={trade.order.orderId}")
        if self._on_fill_callback:
            self._on_fill_callback(trade, fill)

    def _handle_order_status(self, trade):
        logger.info(f"Order status: {trade.order.orderId} -> {trade.orderStatus.status} "
                     f"filled={trade.orderStatus.filled} remaining={trade.orderStatus.remaining}")
        if self._on_status_callback:
            self._on_status_callback(trade)

    def create_contract(self, symbol, exchange="SMART", currency="USD"):
        contract = Stock(symbol, exchange, currency)
        self.ib.qualifyContracts(contract)
        return contract

    def place_limit_order(self, contract, action, quantity, price, parent_id=None, tif="GTC"):
        order = LimitOrder(action, quantity, price, tif=tif)
        if parent_id:
            order.parentId = parent_id
        trade = self.ib.placeOrder(contract, order)
        logger.info(f"Placed limit {action} {quantity} {contract.symbol}@{price} id={trade.order.orderId}")
        return trade

    def place_stop_order(self, contract, action, quantity, stop_price, parent_id=None, tif="GTC"):
        order = StopOrder(action, quantity, stop_price, tif=tif)
        if parent_id:
            order.parentId = parent_id
        trade = self.ib.placeOrder(contract, order)
        logger.info(f"Placed stop {action} {quantity} {contract.symbol}@{stop_price} id={trade.order.orderId}")
        return trade

    def place_trailing_stop(self, contract, action, quantity, trailing_pct, parent_id=None, tif="GTC"):
        order = Order()
        order.action = action
        order.totalQuantity = quantity
        order.orderType = "TRAIL"
        order.trailingPercent = trailing_pct * 100
        order.tif = tif
        if parent_id:
            order.parentId = parent_id
        trade = self.ib.placeOrder(contract, order)
        logger.info(f"Placed trailing stop {action} {quantity} {contract.symbol} trail={trailing_pct*100}% id={trade.order.orderId}")
        return trade

    def modify_order(self, trade, **kwargs):
        order = trade.order
        for key, value in kwargs.items():
            setattr(order, key, value)
        self.ib.placeOrder(trade.contract, order)
        logger.info(f"Modified order {order.orderId}: {kwargs}")

    def cancel_order(self, trade):
        self.ib.cancelOrder(trade.order)
        logger.info(f"Cancelled order {trade.order.orderId}")

    def get_open_orders(self):
        return self.ib.openOrders()

    def get_open_trades(self):
        return self.ib.openTrades()

    def get_positions(self):
        return self.ib.positions()

    def get_account_summary(self):
        account = self.config.get("account", "")
        if account:
            return self.ib.accountSummary(account)
        return self.ib.accountSummary()

    def sleep(self, seconds=0):
        self.ib.sleep(seconds)

    def run(self):
        util.startLoop()
