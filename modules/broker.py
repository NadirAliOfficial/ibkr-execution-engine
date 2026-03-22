import logging
import time
import queue
import threading
from ib_insync import IB, Stock, LimitOrder, StopOrder, Order, Trade

logger = logging.getLogger(__name__)


class IBKRBroker:
    """IBKR broker using a command queue to safely call ib_insync from Flask threads."""

    def __init__(self, config):
        self.config = config["ibkr"]
        self.ib = IB()
        self._connected = False
        self._cmd_queue = queue.Queue()
        self._on_fill_callback = None
        self._on_status_callback = None
        self._on_disconnect_callback = None

    def _execute_in_ib(self, func, *args, **kwargs):
        """Queue a function to run in the IB thread and wait for its result."""
        result_event = threading.Event()
        result_holder = {"value": None, "error": None}

        def _task():
            try:
                result_holder["value"] = func(*args, **kwargs)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()

        self._cmd_queue.put(_task)
        result_event.wait(timeout=30)

        if result_holder["error"]:
            raise result_holder["error"]
        return result_holder["value"]

    def process_queue(self):
        """Process pending commands — called from IB event loop."""
        while not self._cmd_queue.empty():
            try:
                task = self._cmd_queue.get_nowait()
                task()
            except queue.Empty:
                break

    def run_loop(self):
        """Main IB event loop — runs in the main thread."""
        logger.info("IB event loop running")
        while True:
            self.process_queue()
            self.ib.sleep(0.05)

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
            self.ib.disconnectedEvent += lambda: self._handle_disconnect()
            self.ib.execDetailsEvent += lambda trade, fill: self._handle_exec_details(trade, fill)
            self.ib.orderStatusEvent += lambda trade: self._handle_order_status(trade)
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
        try:
            return self._connected and self.ib.client.isConnected()
        except Exception:
            self._connected = False
            return False

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
        def _do():
            contract = Stock(symbol, exchange, currency)
            self.ib.qualifyContracts(contract)
            return contract
        return self._execute_in_ib(_do)

    def place_limit_order(self, contract, action, quantity, price, parent_id=None, tif="GTC"):
        def _do():
            order = LimitOrder(action, quantity, price, tif=tif)
            if parent_id:
                order.parentId = parent_id
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(0.2)
            logger.info(f"Placed limit {action} {quantity} {contract.symbol}@{price} id={trade.order.orderId}")
            return trade
        return self._execute_in_ib(_do)

    def place_stop_order(self, contract, action, quantity, stop_price, parent_id=None, tif="GTC"):
        def _do():
            order = StopOrder(action, quantity, stop_price, tif=tif)
            if parent_id:
                order.parentId = parent_id
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(0.2)
            logger.info(f"Placed stop {action} {quantity} {contract.symbol}@{stop_price} id={trade.order.orderId}")
            return trade
        return self._execute_in_ib(_do)

    def place_trailing_stop(self, contract, action, quantity, trailing_pct, parent_id=None, tif="GTC"):
        def _do():
            order = Order()
            order.action = action
            order.totalQuantity = quantity
            order.orderType = "TRAIL"
            order.trailingPercent = trailing_pct * 100
            order.tif = tif
            if parent_id:
                order.parentId = parent_id
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(0.2)
            logger.info(f"Placed trailing stop {action} {quantity} {contract.symbol} trail={trailing_pct*100}% id={trade.order.orderId}")
            return trade
        return self._execute_in_ib(_do)

    def modify_order(self, trade, **kwargs):
        def _do():
            order = trade.order
            for key, value in kwargs.items():
                setattr(order, key, value)
            self.ib.placeOrder(trade.contract, order)
            self.ib.sleep(0.2)
            logger.info(f"Modified order {order.orderId}: {kwargs}")
        return self._execute_in_ib(_do)

    def cancel_order(self, trade):
        def _do():
            self.ib.cancelOrder(trade.order)
            self.ib.sleep(0.2)
            logger.info(f"Cancelled order {trade.order.orderId}")
        return self._execute_in_ib(_do)

    def get_open_orders(self):
        return self._execute_in_ib(self.ib.openOrders)

    def get_open_trades(self):
        return self._execute_in_ib(self.ib.openTrades)

    def get_positions(self):
        return self._execute_in_ib(self.ib.positions)

    def sleep(self, seconds=0):
        self.ib.sleep(seconds)
