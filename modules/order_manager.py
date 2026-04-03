import json
import logging
import os
from datetime import datetime
from ib_insync import Stock, Order, LimitOrder, StopOrder

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
        self._monitored_tickers = {}  # trade_id -> Ticker (in-memory, reset on restart)

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
            "current_stop_price": stop_price,
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
            "protection_1r_triggered": False,
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

        # Place entry order only — child orders placed after entry fills
        entry_trade = self.broker.place_limit_order(
            contract, trade["side"], trade["total_shares"], trade["entry_price"]
        )
        trade["order_ids"]["entry"] = entry_trade.order.orderId

        trade["state"] = TradeState.ENTRY_PLACED
        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()

        logger.info(f"Trade {trade_id} entry placed: id={trade['order_ids']['entry']}")

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
                self._place_exit_orders(trade)
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
            trade["filled_qty"]["runner"] = filled_qty
            trade["state"] = TradeState.CLOSED
            logger.info(f"Trade {trade_id}: Stop/trailing filled, trade closed")
            self._stop_1r_monitoring(trade_id)
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
            self._stop_1r_monitoring(trade["trade_id"])
            self._cancel_remaining_orders(trade)
            self._save_state()

    def _place_exit_orders(self, trade):
        """Place TP1, TP2, and stop orders after entry fills. Direct IB calls (callback context)."""
        # Guard: don't place exit orders twice
        if "stop" in trade["order_ids"]:
            logger.warning(f"Trade {trade['trade_id']}: Exit orders already placed, skipping")
            return

        # Get qualified contract from the filled entry trade
        contract = None
        for t in self.broker.ib.trades():
            if t.order.orderId == trade["order_ids"]["entry"]:
                contract = t.contract
                break
        if not contract:
            logger.error(f"Trade {trade['trade_id']}: Could not find qualified contract from entry trade")
            contract = Stock(trade["symbol"], "SMART", "USD")
            try:
                self.broker.ib.qualifyContracts(contract)
            except Exception as e:
                logger.error(f"Trade {trade['trade_id']}: Failed to qualify fallback contract: {e}")
                return

        # Place STOP FIRST — position protection is the priority
        try:
            stop_order = StopOrder(trade["exit_side"], trade["total_shares"],
                                   trade["stop_price"], tif="GTC", outsideRth=True)
            stop_trade = self.broker.ib.placeOrder(contract, stop_order)
            trade["order_ids"]["stop"] = stop_trade.order.orderId
            trade["current_stop_price"] = trade["stop_price"]
            logger.info(f"Trade {trade['trade_id']}: Stop placed id={stop_trade.order.orderId} "
                         f"{trade['total_shares']}@{trade['stop_price']}")
            self._save_state()
        except Exception as e:
            logger.error(f"Trade {trade['trade_id']}: CRITICAL — stop order placement failed: {e}")
            self._save_state()
            return

        # TP1
        try:
            tp1_order = LimitOrder(trade["exit_side"], trade["bracket_sizes"]["tp1"],
                                   trade["tp_prices"]["tp1"], tif="GTC", outsideRth=True)
            tp1_trade = self.broker.ib.placeOrder(contract, tp1_order)
            trade["order_ids"]["tp1"] = tp1_trade.order.orderId
            logger.info(f"Trade {trade['trade_id']}: TP1 placed id={tp1_trade.order.orderId} "
                         f"{trade['bracket_sizes']['tp1']}@{trade['tp_prices']['tp1']}")
        except Exception as e:
            logger.error(f"Trade {trade['trade_id']}: TP1 placement failed: {e}")

        # TP2
        try:
            tp2_order = LimitOrder(trade["exit_side"], trade["bracket_sizes"]["tp2"],
                                   trade["tp_prices"]["tp2"], tif="GTC", outsideRth=True)
            tp2_trade = self.broker.ib.placeOrder(contract, tp2_order)
            trade["order_ids"]["tp2"] = tp2_trade.order.orderId
            logger.info(f"Trade {trade['trade_id']}: TP2 placed id={tp2_trade.order.orderId} "
                         f"{trade['bracket_sizes']['tp2']}@{trade['tp_prices']['tp2']}")
        except Exception as e:
            logger.error(f"Trade {trade['trade_id']}: TP2 placement failed: {e}")

        self._save_state()

        # Start 1R monitoring after exit orders are live
        self._start_1r_monitoring(trade, contract)

    def _start_1r_monitoring(self, trade, contract=None):
        """Subscribe to market data for 1R protection monitoring. Direct IB call (main thread)."""
        if trade.get("protection_1r_triggered"):
            return
        trade_id = trade["trade_id"]
        if trade_id in self._monitored_tickers:
            return  # already subscribed

        if contract is None:
            for t in self.broker.ib.trades():
                if t.order.orderId == trade["order_ids"].get("entry"):
                    contract = t.contract
                    break
            if not contract:
                contract = Stock(trade["symbol"], "SMART", "USD")

        try:
            ticker = self.broker.ib.reqMktData(contract, "", False, False)
            self._monitored_tickers[trade_id] = ticker
            entry = trade["entry_price"]
            stop = trade["stop_price"]
            r = abs(entry - stop)
            cfg = self.config.get("protection", {})
            trigger_multiple = cfg.get("one_r_trigger", 1.0)
            trigger_level = (entry + r * trigger_multiple if trade["side"] == "BUY"
                             else entry - r * trigger_multiple)
            logger.info(f"Trade {trade_id}: 1R monitoring started — "
                         f"trigger={'bid' if trade['side'] == 'BUY' else 'ask'}>={trigger_level:.2f}")
        except Exception as e:
            logger.error(f"Trade {trade_id}: Failed to start 1R monitoring: {e}")

    def _stop_1r_monitoring(self, trade_id):
        """Cancel market data subscription for this trade."""
        ticker = self._monitored_tickers.pop(trade_id, None)
        if ticker is not None:
            try:
                self.broker.ib.cancelMktData(ticker.contract)
                logger.info(f"Trade {trade_id}: 1R monitoring stopped")
            except Exception as e:
                logger.warning(f"Trade {trade_id}: Could not cancel market data: {e}")

    def check_1r_protections(self):
        """Evaluate 1R protection for all monitored trades. Called from IB event loop (main thread)."""
        for trade_id, ticker in list(self._monitored_tickers.items()):
            trade = self.trades.get(trade_id)
            if not trade:
                self._stop_1r_monitoring(trade_id)
                continue

            if trade["state"] in (TradeState.CLOSED, TradeState.CANCELLED):
                self._stop_1r_monitoring(trade_id)
                continue

            if trade.get("protection_1r_triggered"):
                self._stop_1r_monitoring(trade_id)
                continue

            bid = ticker.bid
            ask = ticker.ask

            if trade["side"] == "BUY":
                check_price = bid
            else:
                check_price = ask

            # Skip invalid/stale tick values
            if check_price is None or check_price <= 0:
                continue

            self._evaluate_1r(trade, check_price)

    def _evaluate_1r(self, trade, check_price):
        """Check if +1R threshold crossed; move stop if so."""
        entry = trade["entry_price"]
        stop = trade["stop_price"]
        r = abs(entry - stop)
        cfg = self.config.get("protection", {})
        trigger_multiple = cfg.get("one_r_trigger", 1.0)
        offset_r = cfg.get("one_r_stop_offset_r", 0.0)

        if trade["side"] == "BUY":
            trigger_level = entry + r * trigger_multiple
            new_stop = round(entry + r * offset_r, 2)
            triggered = check_price >= trigger_level
            is_protective = new_stop > trade.get("current_stop_price", trade["stop_price"])
        else:
            trigger_level = entry - r * trigger_multiple
            new_stop = round(entry - r * offset_r, 2)
            triggered = check_price <= trigger_level
            is_protective = new_stop < trade.get("current_stop_price", trade["stop_price"])

        if not triggered:
            return

        logger.info(f"Trade {trade['trade_id']}: +1R triggered — "
                     f"{'bid' if trade['side'] == 'BUY' else 'ask'}={check_price:.2f} "
                     f"trigger={trigger_level:.2f}, moving stop to {new_stop:.2f}")

        if not is_protective:
            logger.info(f"Trade {trade['trade_id']}: Stop already at {trade.get('current_stop_price'):.2f} "
                         f">= {new_stop:.2f}, marking triggered without move")
            trade["protection_1r_triggered"] = True
            trade["updated_at"] = datetime.now().isoformat()
            self._save_state()
            self._stop_1r_monitoring(trade["trade_id"])
            return

        self._apply_1r_stop(trade, new_stop)

    def _apply_1r_stop(self, trade, new_stop_price):
        """Modify the active stop order to the 1R protection price. Direct IB call (main thread)."""
        stop_id = trade["order_ids"].get("stop")
        if not stop_id:
            logger.error(f"Trade {trade['trade_id']}: No stop order to move for 1R protection")
            return

        for t in self.broker.ib.openTrades():
            if t.order.orderId == stop_id:
                t.order.auxPrice = new_stop_price
                t.order.outsideRth = True
                self.broker.ib.placeOrder(t.contract, t.order)
                trade["protection_1r_triggered"] = True
                trade["current_stop_price"] = new_stop_price
                trade["updated_at"] = datetime.now().isoformat()
                logger.info(f"Trade {trade['trade_id']}: Stop moved to {new_stop_price:.2f} "
                             f"(1R protection locked)")
                self._save_state()
                self._stop_1r_monitoring(trade["trade_id"])
                return

        logger.warning(f"Trade {trade['trade_id']}: Stop order {stop_id} not found in open trades "
                        f"for 1R protection — marking triggered anyway")
        trade["protection_1r_triggered"] = True
        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()
        self._stop_1r_monitoring(trade["trade_id"])

    def _move_stop_to_breakeven(self, trade):
        if trade["breakeven_moved"]:
            return

        stop_order_id = trade["order_ids"].get("stop")
        if not stop_order_id:
            return

        entry = trade["entry_price"]
        current = trade.get("current_stop_price", trade["stop_price"])

        # Ratchet: only move price if entry is more protective than current stop
        if trade["side"] == "BUY":
            price_move_needed = entry > current
        else:
            price_move_needed = entry < current

        # Direct IB calls — this runs inside an IB callback (main thread)
        for t in self.broker.ib.openTrades():
            if t.order.orderId == stop_order_id:
                new_qty = trade["bracket_sizes"]["tp2"] + trade["bracket_sizes"]["runner"]
                if price_move_needed:
                    t.order.auxPrice = entry
                    new_price = entry
                else:
                    new_price = current
                t.order.totalQuantity = new_qty
                t.order.outsideRth = True
                self.broker.ib.placeOrder(t.contract, t.order)
                trade["breakeven_moved"] = True
                if price_move_needed:
                    trade["current_stop_price"] = entry
                trade["updated_at"] = datetime.now().isoformat()
                logger.info(f"Trade {trade['trade_id']}: Stop updated after TP1 — "
                             f"price={new_price} qty={new_qty}")
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

        # Place trailing stop for runner — get contract from existing trade
        contract = None
        for t in self.broker.ib.trades():
            if t.order.orderId == trade["order_ids"]["entry"]:
                contract = t.contract
                break
        if not contract:
            contract = Stock(trade["symbol"], "SMART", "USD")
        order = Order()
        order.action = trade["exit_side"]
        order.totalQuantity = trade["bracket_sizes"]["runner"]
        order.orderType = "TRAIL"
        order.trailingPercent = trade["trailing_stop_pct"] * 100
        order.tif = "GTC"
        order.outsideRth = True
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
        # Skip closed/cancelled trades — IB reuses order IDs across sessions
        for trade in self.trades.values():
            if trade["state"] in (TradeState.CLOSED, TradeState.CANCELLED):
                continue
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

            # Backfill current_stop_price for trades persisted before this field existed
            if "current_stop_price" not in trade:
                trade["current_stop_price"] = trade["stop_price"]
            if "protection_1r_triggered" not in trade:
                trade["protection_1r_triggered"] = False

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
                    # Place exit orders if they don't exist yet
                    if "tp1" not in trade["order_ids"]:
                        logger.info(f"  Recovery: placing exit orders for {trade_id}")
                        self._place_exit_orders(trade)
                        continue  # _place_exit_orders also starts 1R monitoring

                # Re-subscribe 1R monitoring for filled trades that haven't hit +1R yet
                if (trade["state"] not in (TradeState.CLOSED, TradeState.CANCELLED) and
                        not trade.get("protection_1r_triggered") and
                        trade_id not in self._monitored_tickers):
                    logger.info(f"  Recovery: re-subscribing 1R monitoring for {trade_id}")
                    self._start_1r_monitoring(trade)
            else:
                if trade["state"] not in (TradeState.PENDING, TradeState.ENTRY_PLACED):
                    logger.warning(f"  No position found but trade state is {trade['state']}")
                    trade["state"] = TradeState.CLOSED
                    trade["updated_at"] = datetime.now().isoformat()

        self._save_state()
        logger.info("State recovery complete")

    def verify_stops(self):
        """Check all filled trades have active stop orders. Re-place if missing.
        Called periodically from the IB event loop (main thread)."""
        open_order_ids = set()
        try:
            for t in self.broker.ib.openTrades():
                open_order_ids.add(t.order.orderId)
        except Exception as e:
            logger.warning(f"verify_stops: Could not fetch open trades: {e}")
            return

        for trade_id, trade in self.trades.items():
            if trade["state"] in (TradeState.CLOSED, TradeState.CANCELLED, TradeState.PENDING,
                                  TradeState.ENTRY_PLACED):
                continue

            # Trade is filled — must have a stop or trailing stop active
            stop_id = trade["order_ids"].get("trailing_stop") or trade["order_ids"].get("stop")
            if not stop_id:
                logger.error(f"verify_stops: Trade {trade_id} has no stop order ID — placing stop")
                self._place_exit_orders(trade)
                continue

            if stop_id not in open_order_ids:
                logger.error(f"verify_stops: Trade {trade_id} stop order {stop_id} not active — re-placing")
                # Clear the old stop ID so _place_exit_orders guard doesn't block
                trade["order_ids"].pop("stop", None)
                trade["order_ids"].pop("trailing_stop", None)
                # Also clear TP IDs if they're gone (will be re-placed)
                if trade["order_ids"].get("tp1") and trade["order_ids"]["tp1"] not in open_order_ids:
                    trade["order_ids"].pop("tp1", None)
                if trade["order_ids"].get("tp2") and trade["order_ids"]["tp2"] not in open_order_ids:
                    trade["order_ids"].pop("tp2", None)
                self._place_exit_orders(trade)

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
            "current_stop_price": trade.get("current_stop_price", trade["stop_price"]),
            "total_shares": trade["total_shares"],
            "state": trade["state"],
            "filled": trade["filled_qty"],
            "breakeven_moved": trade["breakeven_moved"],
            "runner_trailing_active": trade["runner_trailing_active"],
            "protection_1r_triggered": trade.get("protection_1r_triggered", False),
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
