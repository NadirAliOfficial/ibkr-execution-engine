import json
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from ib_insync import Stock, Order, LimitOrder, StopOrder

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


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
        self._monitored_tickers = {}   # trade_id -> Ticker (1R monitoring)
        self._runner_tickers = {}      # trade_id -> Ticker (runner trailing monitoring)
        self._session_mode = config.get("session", {}).get("mode", "auto")

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _is_rth(self):
        now = datetime.now(_ET)
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 30) <= t < dtime(16, 0)

    def get_session_state(self):
        return "RTH" if self._is_rth() else "ETH"

    def get_session_mode(self):
        return self._session_mode

    def set_session_mode(self, mode):
        valid = ("auto", "rth_only", "eth_allowed")
        if mode not in valid:
            raise ValueError(f"Invalid session mode: {mode}. Must be one of {valid}")
        self._session_mode = mode
        logger.info(f"Session mode set to: {mode}")

    def _check_session_allowed(self, session_mode):
        if session_mode == "rth_only" and not self._is_rth():
            raise ValueError("RTH-only mode: entries are blocked outside regular trading hours (9:30–16:00 ET)")

    def _entry_outside_rth(self, session_mode):
        """Returns True if entry order should have outsideRth=True."""
        if session_mode == "eth_allowed":
            return True
        if session_mode == "auto" and not self._is_rth():
            return True
        return False

    # ------------------------------------------------------------------
    # Trade creation and execution
    # ------------------------------------------------------------------

    def create_trade(self, symbol, side, entry_price, stop_price, risk_amount,
                     mode=None, session_mode=None):
        mode = (mode or self.config.get("default_mode", "conservative")).lower()
        session_mode = session_mode or self._session_mode

        modes = self.config.get("modes", {})
        mode_cfg = modes.get(mode)
        if mode_cfg is None:
            raise ValueError(f"Unknown mode '{mode}'. Available: {list(modes.keys())}")

        self._check_session_allowed(session_mode)

        total_shares = self.risk.calculate_position_size(entry_price, stop_price, risk_amount)
        bracket_sizes = self.risk.calculate_bracket_sizes(total_shares, mode_cfg)
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
            "mode": mode,
            "session_mode": session_mode,
            "state": TradeState.PENDING,
            "order_ids": {},
            "filled_qty": {"entry": 0, "tp1": 0, "tp2": 0, "runner": 0},
            "breakeven_moved": False,
            "runner_trailing_active": False,
            "runner_activated": False,
            "runner_peak_price": None,
            "runner_tp2_fill_time": None,
            "protection_1r_triggered": False,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        self.trades[trade_id] = trade_record
        self._save_state()

        logger.info(f"Trade created: {trade_id} | {side} {total_shares} {symbol}@{entry_price} "
                     f"stop={stop_price} risk=${risk_amount} mode={mode} session={session_mode}")
        logger.info(f"  Brackets: TP1={bracket_sizes['tp1']}@{tp_prices['tp1']} "
                     f"TP2={bracket_sizes['tp2']}@{tp_prices['tp2']} "
                     f"Runner={bracket_sizes['runner']}")

        return trade_record

    def execute_trade(self, trade_id):
        trade = self.trades[trade_id]
        contract = self.broker.create_contract(trade["symbol"])
        outside_rth = self._entry_outside_rth(trade.get("session_mode", "auto"))

        entry_trade = self.broker.place_limit_order(
            contract, trade["side"], trade["total_shares"], trade["entry_price"],
            outside_rth=outside_rth
        )
        trade["order_ids"]["entry"] = entry_trade.order.orderId

        trade["state"] = TradeState.ENTRY_PLACED
        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()

        logger.info(f"Trade {trade_id} entry placed: id={trade['order_ids']['entry']} "
                     f"outsideRth={outside_rth}")

        return trade

    # ------------------------------------------------------------------
    # Fill and status handling
    # ------------------------------------------------------------------

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
            self._stop_runner_monitoring(trade_id)
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
            self._stop_runner_monitoring(trade["trade_id"])
            self._cancel_remaining_orders(trade)
            self._save_state()

    # ------------------------------------------------------------------
    # Exit order placement
    # ------------------------------------------------------------------

    def _place_exit_orders(self, trade):
        if "stop" in trade["order_ids"]:
            logger.warning(f"Trade {trade['trade_id']}: Exit orders already placed, skipping")
            return

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

        try:
            tp1_order = LimitOrder(trade["exit_side"], trade["bracket_sizes"]["tp1"],
                                   trade["tp_prices"]["tp1"], tif="GTC", outsideRth=True)
            tp1_trade = self.broker.ib.placeOrder(contract, tp1_order)
            trade["order_ids"]["tp1"] = tp1_trade.order.orderId
            logger.info(f"Trade {trade['trade_id']}: TP1 placed id={tp1_trade.order.orderId} "
                         f"{trade['bracket_sizes']['tp1']}@{trade['tp_prices']['tp1']}")
        except Exception as e:
            logger.error(f"Trade {trade['trade_id']}: TP1 placement failed: {e}")

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
        self._start_1r_monitoring(trade, contract)

    # ------------------------------------------------------------------
    # 1R protection monitoring
    # ------------------------------------------------------------------

    def _start_1r_monitoring(self, trade, contract=None):
        if trade.get("protection_1r_triggered"):
            return
        trade_id = trade["trade_id"]
        if trade_id in self._monitored_tickers:
            return

        if contract is None:
            for t in self.broker.ib.trades():
                if t.order.orderId == trade["order_ids"].get("entry"):
                    contract = t.contract
                    break
            if not contract:
                contract = Stock(trade["symbol"], "SMART", "USD")

        try:
            self.broker.ib.reqMarketDataType(3)
            ticker = self.broker.ib.reqMktData(contract, "", False, False)
            self._monitored_tickers[trade_id] = ticker
            entry = trade["entry_price"]
            stop = trade["stop_price"]
            r = abs(entry - stop)
            cfg = self.config.get("protection", {})
            trigger_multiple = cfg.get("one_r_trigger", 1.0)
            trigger_level = (entry + r * trigger_multiple if trade["side"] == "BUY"
                             else entry - r * trigger_multiple)
            direction = ">=" if trade["side"] == "BUY" else "<="
            logger.info(f"Trade {trade_id}: 1R monitoring started — "
                         f"trigger={'bid' if trade['side'] == 'BUY' else 'ask'}{direction}{trigger_level:.2f}")
        except Exception as e:
            logger.error(f"Trade {trade_id}: Failed to start 1R monitoring: {e}")

    def _stop_1r_monitoring(self, trade_id):
        ticker = self._monitored_tickers.pop(trade_id, None)
        if ticker is not None:
            try:
                self.broker.ib.cancelMktData(ticker.contract)
                logger.info(f"Trade {trade_id}: 1R monitoring stopped")
            except Exception as e:
                logger.warning(f"Trade {trade_id}: Could not cancel 1R market data: {e}")

    def check_1r_protections(self):
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

            check_price = self._get_check_price(trade, ticker)
            if check_price is None:
                continue

            self._evaluate_1r(trade, check_price)

    def _evaluate_1r(self, trade, check_price):
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

    # ------------------------------------------------------------------
    # Runner trailing monitoring
    # ------------------------------------------------------------------

    def _activate_runner_trailing(self, trade):
        if trade["runner_trailing_active"]:
            return

        runner_qty = trade["bracket_sizes"]["runner"]

        # Scalp / 0% runner: close out cleanly after TP2
        if runner_qty == 0:
            self._stop_1r_monitoring(trade["trade_id"])
            stop_id = trade["order_ids"].get("stop")
            if stop_id:
                for t in self.broker.ib.openTrades():
                    if t.order.orderId == stop_id:
                        self.broker.ib.cancelOrder(t.order)
                        break
            trade["state"] = TradeState.CLOSED
            trade["runner_trailing_active"] = True
            trade["updated_at"] = datetime.now().isoformat()
            logger.info(f"Trade {trade['trade_id']}: Runner allocation is 0% (Scalp) — "
                         f"trade closed after TP2")
            self._save_state()
            return

        # Update stop qty to runner only
        stop_id = trade["order_ids"].get("stop")
        if stop_id:
            for t in self.broker.ib.openTrades():
                if t.order.orderId == stop_id:
                    t.order.totalQuantity = runner_qty
                    t.order.outsideRth = True
                    self.broker.ib.placeOrder(t.contract, t.order)
                    logger.info(f"Trade {trade['trade_id']}: Stop qty updated to runner={runner_qty} "
                                 f"after TP2 fill")
                    break

        trade["runner_tp2_fill_time"] = datetime.now().isoformat()
        trade["runner_activated"] = False
        trade["runner_peak_price"] = None
        trade["runner_trailing_active"] = True
        trade["updated_at"] = datetime.now().isoformat()
        self._save_state()

        self._start_runner_monitoring(trade)

        runner_cfg = self.config.get("runner", {})
        activation_type = runner_cfg.get("activation_type", "price")
        logger.info(f"Trade {trade['trade_id']}: Runner monitoring started — "
                     f"activation_type={activation_type} awaiting trigger")

    def _start_runner_monitoring(self, trade, contract=None):
        trade_id = trade["trade_id"]
        if trade_id in self._runner_tickers:
            return

        if contract is None:
            for t in self.broker.ib.trades():
                if t.order.orderId == trade["order_ids"].get("entry"):
                    contract = t.contract
                    break
            if not contract:
                contract = Stock(trade["symbol"], "SMART", "USD")

        try:
            # Reuse existing 1R ticker if available to avoid duplicate subscriptions
            existing = self._monitored_tickers.get(trade_id)
            if existing is not None:
                self._runner_tickers[trade_id] = existing
            else:
                self.broker.ib.reqMarketDataType(3)
                ticker = self.broker.ib.reqMktData(contract, "", False, False)
                self._runner_tickers[trade_id] = ticker
        except Exception as e:
            logger.error(f"Trade {trade_id}: Failed to start runner monitoring: {e}")

    def _stop_runner_monitoring(self, trade_id):
        ticker = self._runner_tickers.pop(trade_id, None)
        if ticker is not None:
            # Only cancel market data if not also used by 1R monitoring
            if trade_id not in self._monitored_tickers:
                try:
                    self.broker.ib.cancelMktData(ticker.contract)
                except Exception as e:
                    logger.warning(f"Trade {trade_id}: Could not cancel runner market data: {e}")
            logger.info(f"Trade {trade_id}: Runner monitoring stopped")

    def check_runner_trailing(self):
        for trade_id, ticker in list(self._runner_tickers.items()):
            trade = self.trades.get(trade_id)
            if not trade:
                self._stop_runner_monitoring(trade_id)
                continue

            if trade["state"] in (TradeState.CLOSED, TradeState.CANCELLED):
                self._stop_runner_monitoring(trade_id)
                continue

            check_price = self._get_check_price(trade, ticker)
            if check_price is None:
                continue

            self._evaluate_runner(trade, check_price)

    def _evaluate_runner(self, trade, check_price):
        runner_cfg = self.config.get("runner", {})
        activation_type = runner_cfg.get("activation_type", "price")
        price_ext_r = runner_cfg.get("price_extension_r", 0.25)
        time_delay = runner_cfg.get("time_delay_seconds", 20)
        trailing_r = runner_cfg.get("trailing_distance_r", 0.25)

        entry = trade["entry_price"]
        stop = trade["stop_price"]
        r = abs(entry - stop)
        tp2_price = trade["tp_prices"]["tp2"]
        is_long = trade["side"] == "BUY"

        if not trade.get("runner_activated"):
            activated = False
            if activation_type == "price":
                if is_long:
                    activated = check_price >= tp2_price + r * price_ext_r
                else:
                    activated = check_price <= tp2_price - r * price_ext_r
            else:
                fill_time_str = trade.get("runner_tp2_fill_time")
                if fill_time_str:
                    elapsed = (datetime.now() - datetime.fromisoformat(fill_time_str)).total_seconds()
                    activated = elapsed >= time_delay

            if activated:
                trade["runner_activated"] = True
                trade["runner_peak_price"] = check_price
                trade["state"] = TradeState.RUNNER_ACTIVE
                trade["updated_at"] = datetime.now().isoformat()
                logger.info(f"Trade {trade['trade_id']}: Runner activated — "
                             f"{'bid' if is_long else 'ask'}={check_price:.2f} "
                             f"activation_type={activation_type}")
                self._save_state()
            return

        # Runner active — update peak and ratchet stop
        peak = trade.get("runner_peak_price") or check_price

        if is_long:
            if check_price > peak:
                trade["runner_peak_price"] = check_price
                peak = check_price
            new_stop = round(peak - r * trailing_r, 2)
            is_protective = new_stop > trade.get("current_stop_price", trade["stop_price"])
        else:
            if check_price < peak:
                trade["runner_peak_price"] = check_price
                peak = check_price
            new_stop = round(peak + r * trailing_r, 2)
            is_protective = new_stop < trade.get("current_stop_price", trade["stop_price"])

        if not is_protective:
            return

        stop_id = trade["order_ids"].get("stop")
        if not stop_id:
            return

        for t in self.broker.ib.openTrades():
            if t.order.orderId == stop_id:
                t.order.auxPrice = new_stop
                t.order.outsideRth = True
                self.broker.ib.placeOrder(t.contract, t.order)
                trade["current_stop_price"] = new_stop
                trade["updated_at"] = datetime.now().isoformat()
                logger.info(f"Trade {trade['trade_id']}: Runner trailing stop updated to {new_stop:.2f} "
                             f"(peak={'bid' if is_long else 'ask'}={peak:.2f})")
                self._save_state()
                return

    # ------------------------------------------------------------------
    # Stop to breakeven
    # ------------------------------------------------------------------

    def _move_stop_to_breakeven(self, trade):
        if trade["breakeven_moved"]:
            return

        stop_order_id = trade["order_ids"].get("stop")
        if not stop_order_id:
            return

        entry = trade["entry_price"]
        current = trade.get("current_stop_price", trade["stop_price"])

        if trade["side"] == "BUY":
            price_move_needed = entry > current
        else:
            price_move_needed = entry < current

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_check_price(self, trade, ticker):
        bid = ticker.bid
        ask = ticker.ask
        last = ticker.last

        def _valid(v):
            return v is not None and v == v and v > 0

        if trade["side"] == "BUY":
            check_price = bid if _valid(bid) else (last if _valid(last) else None)
        else:
            check_price = ask if _valid(ask) else (last if _valid(last) else None)

        if check_price is None:
            for item in self.broker.ib.portfolio():
                if item.contract.symbol == trade["symbol"] and _valid(item.marketPrice):
                    check_price = item.marketPrice
                    break

        return check_price

    def _cancel_remaining_orders(self, trade):
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

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

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

            # Backfill fields for trades persisted before 2A.2
            if "current_stop_price" not in trade:
                trade["current_stop_price"] = trade["stop_price"]
            if "protection_1r_triggered" not in trade:
                trade["protection_1r_triggered"] = False
            if "mode" not in trade:
                trade["mode"] = self.config.get("default_mode", "conservative")
            if "session_mode" not in trade:
                trade["session_mode"] = "auto"
            if "runner_activated" not in trade:
                trade["runner_activated"] = False
            if "runner_peak_price" not in trade:
                trade["runner_peak_price"] = None
            if "runner_tp2_fill_time" not in trade:
                trade["runner_tp2_fill_time"] = None

            symbol = trade["symbol"]
            logger.info(f"Recovering trade {trade_id} (state={trade['state']})")

            for order_type, oid in trade["order_ids"].items():
                if oid in open_trades:
                    logger.info(f"  {order_type} order {oid} still active")
                else:
                    logger.info(f"  {order_type} order {oid} no longer active")

            if symbol in positions:
                pos = positions[symbol]
                logger.info(f"  Position: {pos.position} shares @ avg {pos.avgCost}")

                entry_oid = trade["order_ids"].get("entry")
                if trade["state"] == TradeState.ENTRY_PLACED and entry_oid not in open_trades:
                    trade["filled_qty"]["entry"] = trade["total_shares"]
                    trade["state"] = TradeState.ENTRY_FILLED
                    trade["updated_at"] = datetime.now().isoformat()
                    logger.info(f"  Recovery: entry filled, state -> entry_filled")
                    if "tp1" not in trade["order_ids"]:
                        logger.info(f"  Recovery: placing exit orders for {trade_id}")
                        self._place_exit_orders(trade)
                        continue

                filled_states = {TradeState.ENTRY_FILLED, TradeState.TP1_FILLED}
                if (trade["state"] in filled_states and
                        not trade.get("protection_1r_triggered") and
                        trade_id not in self._monitored_tickers):
                    logger.info(f"  Recovery: re-subscribing 1R monitoring for {trade_id}")
                    self._start_1r_monitoring(trade)

                # Re-subscribe runner monitoring for TP2_FILLED / RUNNER_ACTIVE trades
                runner_states = {TradeState.TP2_FILLED, TradeState.RUNNER_ACTIVE}
                if (trade["state"] in runner_states and
                        trade.get("runner_trailing_active") and
                        trade["bracket_sizes"].get("runner", 0) > 0 and
                        trade_id not in self._runner_tickers):
                    logger.info(f"  Recovery: re-subscribing runner monitoring for {trade_id}")
                    self._start_runner_monitoring(trade)
            else:
                if trade["state"] not in (TradeState.PENDING, TradeState.ENTRY_PLACED):
                    logger.warning(f"  No position found but trade state is {trade['state']}")
                    trade["state"] = TradeState.CLOSED
                    trade["updated_at"] = datetime.now().isoformat()

        self._save_state()
        logger.info("State recovery complete")

    # ------------------------------------------------------------------
    # Stop verification
    # ------------------------------------------------------------------

    def verify_stops(self):
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

            stop_id = trade["order_ids"].get("trailing_stop") or trade["order_ids"].get("stop")
            if not stop_id:
                logger.error(f"verify_stops: Trade {trade_id} has no stop order ID — placing stop")
                self._place_exit_orders(trade)
                continue

            if stop_id not in open_order_ids:
                logger.error(f"verify_stops: Trade {trade_id} stop order {stop_id} not active — re-placing")
                state = trade["state"]
                if state == TradeState.ENTRY_FILLED:
                    stop_qty = trade["total_shares"]
                elif state == TradeState.TP1_FILLED:
                    stop_qty = trade["bracket_sizes"]["tp2"] + trade["bracket_sizes"]["runner"]
                else:
                    stop_qty = trade["bracket_sizes"]["runner"]
                stop_price = trade.get("current_stop_price", trade["stop_price"])

                contract = None
                for t in self.broker.ib.trades():
                    if t.order.orderId == trade["order_ids"].get("entry"):
                        contract = t.contract
                        break
                if not contract:
                    contract = Stock(trade["symbol"], "SMART", "USD")

                try:
                    stop_order = StopOrder(trade["exit_side"], stop_qty, stop_price,
                                          tif="GTC", outsideRth=True)
                    stop_trade = self.broker.ib.placeOrder(contract, stop_order)
                    trade["order_ids"]["stop"] = stop_trade.order.orderId
                    trade["order_ids"].pop("trailing_stop", None)
                    trade["current_stop_price"] = stop_price
                    trade["updated_at"] = datetime.now().isoformat()
                    logger.info(f"verify_stops: Re-placed stop for {trade_id} "
                                f"qty={stop_qty}@{stop_price} id={stop_trade.order.orderId}")
                    self._save_state()
                except Exception as e:
                    logger.error(f"verify_stops: Failed to re-place stop for {trade_id}: {e}")

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

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
            "runner_activated": trade.get("runner_activated", False),
            "protection_1r_triggered": trade.get("protection_1r_triggered", False),
            "tp_prices": trade["tp_prices"],
            "bracket_sizes": trade["bracket_sizes"],
            "mode": trade.get("mode", "conservative"),
            "session_mode": trade.get("session_mode", "auto"),
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
