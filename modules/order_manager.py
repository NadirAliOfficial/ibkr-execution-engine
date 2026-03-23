import json
import logging
import os
from datetime import datetime
from ib_insync import Stock, Order

logger = logging.getLogger(__name__)


class TradeState:
    PENDING = "pending"
    ENTRY_PLACED = "entry_placed"
    ENTRY_FILLED = "entry_filled"
    TP1_FILLED = "tp1_filled"
    TP2_FILLED = "tp2_filled"
    RUNNER_ACTIVE = "runner_active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OrderManager:

    def __init__(self, config, broker, risk_manager):
        self.config = config
        self.broker = broker
        self.risk = risk_manager
        self.persistence_file = config["state"]["persistence_file"]
        self.trades = {}

    def create_trade(self, symbol, side, entry_price, stop_price, risk_amount):
        total_shares = self.risk.calculate_position_size(entry_price, stop_price, risk_amount)
        bracket_sizes = self.risk.calculate_bracket_sizes(total_shares)
        tp_prices = self.risk.calculate_tp_prices(entry_price, stop_price, side)
        exit_side = "SELL" if side == "BUY" else "BUY"

        trade_id = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        trade_record = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "exit_side": exit_side,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "risk_amount": risk_amount,
            "total_shares": total_shares,
            "bracket_sizes": bracket_sizes,
            "tp_prices": tp_prices,
            "trailing_stop_pct": self.risk.get_trailing_stop_pct(),
            "state": TradeState.PENDING,
            "order_ids": {},
            "filled_qty": {"entry": 0, "tp1": 0, "tp2": 0, "runner": 0},
            "breakeven_moved": False,
            "runner_trailing_active": False,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        self.trades[trade_id] = trade_record
        self._save_state()

        logger.info(f"Trade created: {trade_id} | {side} {total_shares} {symbol}@{entry_price} "
                     f"stop={stop_price} risk=${risk_amount}")
        logger.info(f"  Brackets: TP1={bracket_sizes['tp1']}@{tp_prices['tp1']} "
                     f"TP2={bracket_sizes['tp2']}@{tp_prices['tp2']} "
                     f"Runner={bracket_sizes['runner']}")

        return trade_record

    def execute_trade(self, trade_id):
        trade = self.trades[trade_id]
        contract = self.broker.create_contract(trade["symbol"])

        # Place entry order
        entry_trade = self.broker.place_limit_order(
            contract, trade["side"], trade["total_shares"], trade["entry_price"]
        )
        parent_id = entry_trade.order.orderId
        trade["order_ids"]["entry"] = parent_id

        # Place TP1 limit order
        tp1_trade = self.broker.place_limit_order(
            contract, trade["exit_side"], trade["bracket_sizes"]["tp1"],
            trade["tp_prices"]["tp1"], parent_id=parent_id
        )
        trade["order_ids"]["tp1"] = tp1_trade.order.orderId

        # Place TP2 limit order
        tp2_trade = self.broker.place_limit_order(
            contract, trade["exit_side"], trade["bracket_sizes"]["tp2"],
            trade["tp_prices"]["tp2"], parent_id=parent_id
        )
        trade["order_ids"]["tp2"] = tp2_trade.order.orderId

        # Place initial stop loss (covers full remaining position)
        stop_qty = trade["total_shares"]
        stop_trade = self.broker.place_stop_order(
            contract, trade["exit_side"], stop_qty,
            trade["stop_price"], parent_id=parent_id
        )
        trade["order_ids"]["stop"] = stop_trade.order.orderId

        trade["state"] = TradeState.ENTRY_PLACED
        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()

        logger.info(f"Trade {trade_id} orders placed: entry={parent_id} "
                     f"tp1={trade['order_ids']['tp1']} tp2={trade['order_ids']['tp2']} "
                     f"stop={trade['order_ids']['stop']}")

        return trade

    def handle_fill(self, order_id, filled_qty, avg_price):
        trade = self._find_trade_by_order(order_id)
        if not trade:
            return

        trade_id = trade["trade_id"]
        order_type = self._get_order_type(trade, order_id)

        if order_type == "entry":
            trade["filled_qty"]["entry"] = filled_qty
            if filled_qty >= trade["total_shares"]:
                trade["state"] = TradeState.ENTRY_FILLED
                logger.info(f"Trade {trade_id}: Entry fully filled {filled_qty}@{avg_price}")
            else:
                logger.info(f"Trade {trade_id}: Entry partial fill {filled_qty}/{trade['total_shares']}@{avg_price}")

        elif order_type == "tp1":
            trade["filled_qty"]["tp1"] = filled_qty
            if filled_qty >= trade["bracket_sizes"]["tp1"]:
                trade["state"] = TradeState.TP1_FILLED
                logger.info(f"Trade {trade_id}: TP1 fully filled {filled_qty}@{avg_price}")
                self._move_stop_to_breakeven(trade)
            else:
                logger.info(f"Trade {trade_id}: TP1 partial fill {filled_qty}/{trade['bracket_sizes']['tp1']}")

        elif order_type == "tp2":
            trade["filled_qty"]["tp2"] = filled_qty
            if filled_qty >= trade["bracket_sizes"]["tp2"]:
                trade["state"] = TradeState.TP2_FILLED
                logger.info(f"Trade {trade_id}: TP2 fully filled {filled_qty}@{avg_price}")
                self._activate_runner_trailing(trade)
            else:
                logger.info(f"Trade {trade_id}: TP2 partial fill {filled_qty}/{trade['bracket_sizes']['tp2']}")

        elif order_type == "stop" or order_type == "trailing_stop":
            runner_qty = trade["bracket_sizes"]["runner"]
            remaining_tp2 = trade["bracket_sizes"]["tp2"] - trade["filled_qty"]["tp2"]
            expected_stop_fill = runner_qty + remaining_tp2
            trade["filled_qty"]["runner"] = filled_qty
            trade["state"] = TradeState.CLOSED
            logger.info(f"Trade {trade_id}: Stop/trailing filled, trade closed")
            self._cancel_remaining_orders(trade)

        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()

    def handle_order_status(self, order_id, status):
        trade = self._find_trade_by_order(order_id)
        if not trade:
            return

        order_type = self._get_order_type(trade, order_id)
        logger.info(f"Trade {trade['trade_id']}: {order_type} order {order_id} -> {status}")

        if status == "Cancelled" and order_type == "entry":
            trade["state"] = TradeState.CANCELLED
            trade["updated_at"] = datetime.now().isoformat()
            self._cancel_remaining_orders(trade)
            self._save_state()

    def _move_stop_to_breakeven(self, trade):
        if trade["breakeven_moved"]:
            return

        stop_order_id = trade["order_ids"].get("stop")
        if not stop_order_id:
            return

        # Direct IB calls — this runs inside an IB callback (main thread)
        for t in self.broker.ib.openTrades():
            if t.order.orderId == stop_order_id:
                new_qty = trade["bracket_sizes"]["tp2"] + trade["bracket_sizes"]["runner"]
                t.order.auxPrice = trade["entry_price"]
                t.order.totalQuantity = new_qty
                self.broker.ib.placeOrder(t.contract, t.order)
                trade["breakeven_moved"] = True
                trade["updated_at"] = datetime.now().isoformat()
                logger.info(f"Trade {trade['trade_id']}: Stop moved to breakeven @{trade['entry_price']} qty={new_qty}")
                self._save_state()
                return

        logger.warning(f"Trade {trade['trade_id']}: Could not find stop order to move to breakeven")

    def _activate_runner_trailing(self, trade):
        if trade["runner_trailing_active"]:
            return

        stop_order_id = trade["order_ids"].get("stop")

        # Direct IB calls — this runs inside an IB callback (main thread)
        # Cancel existing stop
        if stop_order_id:
            for t in self.broker.ib.openTrades():
                if t.order.orderId == stop_order_id:
                    self.broker.ib.cancelOrder(t.order)
                    break

        # Place trailing stop for runner
        contract = Stock(trade["symbol"], "SMART", "USD")
        self.broker.ib.qualifyContracts(contract)
        order = Order()
        order.action = trade["exit_side"]
        order.totalQuantity = trade["bracket_sizes"]["runner"]
        order.orderType = "TRAIL"
        order.trailingPercent = trade["trailing_stop_pct"] * 100
        order.tif = "GTC"
        trailing_trade = self.broker.ib.placeOrder(contract, order)
        trade["order_ids"]["trailing_stop"] = trailing_trade.order.orderId
        trade["runner_trailing_active"] = True
        trade["state"] = TradeState.RUNNER_ACTIVE
        trade["updated_at"] = datetime.now().isoformat()
        logger.info(f"Trade {trade['trade_id']}: Runner trailing stop activated "
                     f"qty={trade['bracket_sizes']['runner']} trail={trade['trailing_stop_pct']*100}%")
        self._save_state()

    def _cancel_remaining_orders(self, trade):
        # Direct IB calls — this runs inside an IB callback (main thread)
        open_trades = self.broker.ib.openTrades()

        all_order_ids = set(trade["order_ids"].values())
        for t in open_trades:
            if t.order.orderId in all_order_ids:
                try:
                    self.broker.ib.cancelOrder(t.order)
                except Exception as e:
                    logger.warning(f"Could not cancel order {t.order.orderId}: {e}")

    def _find_trade_by_order(self, order_id):
        for trade in self.trades.values():
            if order_id in trade["order_ids"].values():
                return trade
        return None

    def _get_order_type(self, trade, order_id):
        for key, oid in trade["order_ids"].items():
            if oid == order_id:
                return key
        return "unknown"

    def recover_state(self):
        self._load_state()
        if not self.trades:
            logger.info("No trade state to recover")
            return

        try:
            ib_trades = self.broker.ib.openTrades()
            ib_positions = self.broker.ib.positions()
        except Exception as e:
            logger.warning(f"Could not fetch IB state for recovery: {e}")
            ib_trades = []
            ib_positions = []

        open_trades = {t.order.orderId: t for t in ib_trades}
        positions = {p.contract.symbol: p for p in ib_positions}

        for trade_id, trade in self.trades.items():
            if trade["state"] in (TradeState.CLOSED, TradeState.CANCELLED):
                continue

            symbol = trade["symbol"]
            logger.info(f"Recovering trade {trade_id} (state={trade['state']})")

            # Verify orders still exist
            active_orders = []
            for order_type, oid in trade["order_ids"].items():
                if oid in open_trades:
                    active_orders.append(order_type)
                    logger.info(f"  {order_type} order {oid} still active")
                else:
                    logger.info(f"  {order_type} order {oid} no longer active")

            # Check position
            if symbol in positions:
                pos = positions[symbol]
                logger.info(f"  Position: {pos.position} shares @ avg {pos.avgCost}")

                # If entry order is no longer active but we have a position, entry was filled
                entry_oid = trade["order_ids"].get("entry")
                if trade["state"] == TradeState.ENTRY_PLACED and entry_oid not in open_trades:
                    trade["filled_qty"]["entry"] = trade["total_shares"]
                    trade["state"] = TradeState.ENTRY_FILLED
                    trade["updated_at"] = datetime.now().isoformat()
                    logger.info(f"  Recovery: entry filled, state -> entry_filled")
            else:
                if trade["state"] not in (TradeState.PENDING, TradeState.ENTRY_PLACED):
                    logger.warning(f"  No position found but trade state is {trade['state']}")
                    trade["state"] = TradeState.CLOSED
                    trade["updated_at"] = datetime.now().isoformat()

        self._save_state()
        logger.info("State recovery complete")

    def _save_state(self):
        os.makedirs(os.path.dirname(self.persistence_file), exist_ok=True)
        with open(self.persistence_file, "w") as f:
            json.dump(self.trades, f, indent=2)

    def _load_state(self):
        if os.path.exists(self.persistence_file):
            with open(self.persistence_file, "r") as f:
                self.trades = json.load(f)
            logger.info(f"Loaded {len(self.trades)} trades from state file")
        else:
            self.trades = {}

    def get_trade_status(self, trade_id):
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        return {
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "side": trade["side"],
            "entry_price": trade["entry_price"],
            "stop_price": trade["stop_price"],
            "total_shares": trade["total_shares"],
            "state": trade["state"],
            "filled": trade["filled_qty"],
            "breakeven_moved": trade["breakeven_moved"],
            "runner_trailing_active": trade["runner_trailing_active"],
            "tp_prices": trade["tp_prices"],
            "bracket_sizes": trade["bracket_sizes"],
            "updated_at": trade["updated_at"],
        }

    def get_all_trades(self):
        return {tid: self.get_trade_status(tid) for tid in self.trades}

    def get_active_trades(self):
        return {
            tid: self.get_trade_status(tid)
            for tid, t in self.trades.items()
            if t["state"] not in (TradeState.CLOSED, TradeState.CANCELLED)
        }
