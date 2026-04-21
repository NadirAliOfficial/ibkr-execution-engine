import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

from modules.order_manager import OrderManager, TradeState

_ET = ZoneInfo("America/New_York")

CONFIG = {
    "state": {"persistence_file": "/tmp/test_trades_unit.json"},
    "defaults": {"trailing_stop_pct": 0.02},
    "modes": {
        "conservative": {"tp1_pct": 0.50, "tp2_pct": 0.40, "runner_pct": 0.10},
        "gapper":       {"tp1_pct": 0.20, "tp2_pct": 0.25, "runner_pct": 0.55},
        "scalp":        {"tp1_pct": 0.75, "tp2_pct": 0.25, "runner_pct": 0.00},
    },
    "default_mode": "conservative",
    "runner": {
        "activation_type": "price",
        "price_extension_r": 0.25,
        "time_delay_seconds": 20,
        "trailing_distance_r": 0.25,
    },
    "protection": {
        "one_r_trigger": 1.0,
        "one_r_stop_offset_r": 0.10,
    },
    "session": {"mode": "auto"},
}

# Mondays during market hours / pre-market / weekend
_RTH_DT  = datetime(2026, 4, 20, 10, 0, 0, tzinfo=_ET)   # Mon 10:00 AM ET
_ETH_DT  = datetime(2026, 4, 20,  7, 0, 0, tzinfo=_ET)   # Mon 7:00 AM ET
_WKND_DT = datetime(2026, 4, 19, 10, 0, 0, tzinfo=_ET)   # Sat 10:00 AM ET


def make_om():
    broker = MagicMock()
    broker.ib.portfolio.return_value = []
    broker.ib.openTrades.return_value = []
    broker.ib.trades.return_value = []
    risk = MagicMock()
    risk.get_trailing_stop_pct.return_value = 0.02
    om = OrderManager(CONFIG, broker, risk)
    om._save_state = MagicMock()
    return om, broker


def make_trade(side="BUY", entry=100.0, stop=98.0, mode="conservative"):
    r = abs(entry - stop)
    runner_qty = 5 if mode != "scalp" else 0
    tp1_price = round(entry + r * 1.5, 2) if side == "BUY" else round(entry - r * 1.5, 2)
    tp2_price = round(entry + r * 2.5, 2) if side == "BUY" else round(entry - r * 2.5, 2)
    return {
        "trade_id": "TEST_001",
        "symbol": "AAPL",
        "side": side,
        "exit_side": "SELL" if side == "BUY" else "BUY",
        "entry_price": entry,
        "stop_price": stop,
        "current_stop_price": stop,
        "risk_amount": 100.0,
        "total_shares": 50,
        "bracket_sizes": {"tp1": 25, "tp2": 20, "runner": runner_qty, "total": 50},
        "tp_prices": {"tp1": tp1_price, "tp2": tp2_price},
        "mode": mode,
        "session_mode": "auto",
        "state": TradeState.ENTRY_FILLED,
        "order_ids": {"entry": 1, "stop": 2, "tp1": 3, "tp2": 4},
        "filled_qty": {"entry": 50, "tp1": 0, "tp2": 0, "runner": 0},
        "breakeven_moved": False,
        "runner_trailing_active": False,
        "runner_activated": False,
        "runner_peak_price": None,
        "runner_tp2_fill_time": None,
        "protection_1r_triggered": False,
        "trailing_stop_pct": 0.02,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

class TestSessionHelpers:
    def setup_method(self):
        self.om, _ = make_om()

    @patch.object(OrderManager, "_is_rth", return_value=True)
    def test_get_session_state_rth(self, _):
        assert self.om.get_session_state() == "RTH"

    @patch.object(OrderManager, "_is_rth", return_value=False)
    def test_get_session_state_eth(self, _):
        assert self.om.get_session_state() == "ETH"

    @patch("modules.order_manager.datetime")
    def test_is_rth_during_market_hours(self, mock_dt):
        mock_dt.now.return_value = _RTH_DT
        assert self.om._is_rth() is True

    @patch("modules.order_manager.datetime")
    def test_is_rth_pre_market(self, mock_dt):
        mock_dt.now.return_value = _ETH_DT
        assert self.om._is_rth() is False

    @patch("modules.order_manager.datetime")
    def test_is_rth_weekend(self, mock_dt):
        mock_dt.now.return_value = _WKND_DT
        assert self.om._is_rth() is False

    @patch.object(OrderManager, "_is_rth", return_value=False)
    def test_rth_only_blocks_outside_rth(self, _):
        with pytest.raises(ValueError, match="RTH-only"):
            self.om._check_session_allowed("rth_only")

    @patch.object(OrderManager, "_is_rth", return_value=True)
    def test_rth_only_passes_inside_rth(self, _):
        self.om._check_session_allowed("rth_only")  # no raise

    def test_set_session_mode_valid(self):
        self.om.set_session_mode("eth_allowed")
        assert self.om.get_session_mode() == "eth_allowed"

    def test_set_session_mode_invalid_raises(self):
        with pytest.raises(ValueError):
            self.om.set_session_mode("bad_mode")

    @patch.object(OrderManager, "_is_rth", return_value=True)
    def test_entry_outside_rth_eth_allowed_always_true(self, _):
        assert self.om._entry_outside_rth("eth_allowed") is True

    @patch.object(OrderManager, "_is_rth", return_value=True)
    def test_entry_outside_rth_auto_during_rth(self, _):
        assert self.om._entry_outside_rth("auto") is False

    @patch.object(OrderManager, "_is_rth", return_value=False)
    def test_entry_outside_rth_auto_outside_rth(self, _):
        assert self.om._entry_outside_rth("auto") is True

    @patch.object(OrderManager, "_is_rth", return_value=False)
    def test_entry_outside_rth_rth_only_outside_rth(self, _):
        assert self.om._entry_outside_rth("rth_only") is False


# ---------------------------------------------------------------------------
# 1R protection
# ---------------------------------------------------------------------------

class TestEvaluate1R:
    def setup_method(self):
        self.om, self.broker = make_om()

    def _mock_stop_trade(self, order_id=2):
        t = MagicMock()
        t.order.orderId = order_id
        return t

    def test_buy_no_trigger_below_1r(self):
        trade = make_trade(side="BUY", entry=100.0, stop=98.0)
        # r=2, trigger=102, price=101 → no trigger
        self.om._evaluate_1r(trade, 101.0)
        assert not trade["protection_1r_triggered"]

    def test_buy_triggers_at_exactly_1r(self):
        trade = make_trade(side="BUY", entry=100.0, stop=98.0)
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()
        self.broker.ib.openTrades.return_value = [self._mock_stop_trade()]

        self.om._evaluate_1r(trade, 102.0)

        assert trade["protection_1r_triggered"] is True
        assert trade["current_stop_price"] == round(100.0 + 2.0 * 0.10, 2)  # 100.20

    def test_buy_triggers_above_1r(self):
        trade = make_trade(side="BUY", entry=100.0, stop=98.0)
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()
        self.broker.ib.openTrades.return_value = [self._mock_stop_trade()]

        self.om._evaluate_1r(trade, 103.0)

        assert trade["protection_1r_triggered"] is True

    def test_buy_already_protective_stop_marks_triggered_without_move(self):
        trade = make_trade(side="BUY", entry=100.0, stop=98.0)
        trade["current_stop_price"] = 101.0  # above new_stop (100.20)
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()

        self.om._evaluate_1r(trade, 102.0)

        assert trade["protection_1r_triggered"] is True
        self.broker.ib.placeOrder.assert_not_called()

    def test_sell_no_trigger_above_1r(self):
        trade = make_trade(side="SELL", entry=100.0, stop=102.0)
        # r=2, trigger=98, price=99 → no trigger
        self.om._evaluate_1r(trade, 99.0)
        assert not trade["protection_1r_triggered"]

    def test_sell_triggers_at_1r(self):
        trade = make_trade(side="SELL", entry=100.0, stop=102.0)
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()
        self.broker.ib.openTrades.return_value = [self._mock_stop_trade()]

        self.om._evaluate_1r(trade, 98.0)

        assert trade["protection_1r_triggered"] is True
        assert trade["current_stop_price"] == round(100.0 - 2.0 * 0.10, 2)  # 99.80

    def test_sell_already_protective_stop(self):
        trade = make_trade(side="SELL", entry=100.0, stop=102.0)
        trade["current_stop_price"] = 99.0  # already below new_stop (99.80)
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()

        self.om._evaluate_1r(trade, 98.0)

        assert trade["protection_1r_triggered"] is True
        self.broker.ib.placeOrder.assert_not_called()

    def test_already_triggered_skips(self):
        trade = make_trade(side="BUY", entry=100.0, stop=98.0)
        trade["protection_1r_triggered"] = True
        self.om._evaluate_1r(trade, 110.0)
        # Would have triggered again — verify placeOrder not called
        self.broker.ib.placeOrder.assert_not_called()


# ---------------------------------------------------------------------------
# Runner trailing
# ---------------------------------------------------------------------------

class TestEvaluateRunner:
    def setup_method(self):
        self.om, self.broker = make_om()

    def _tp2_trade(self, side="BUY", runner_qty=5):
        trade = make_trade(side=side, entry=100.0, stop=98.0)
        # tp2_price = 105.0 for BUY (100 + 2*2.5)
        trade["state"] = TradeState.TP2_FILLED
        trade["runner_trailing_active"] = True
        trade["runner_activated"] = False
        trade["runner_tp2_fill_time"] = datetime.now().isoformat()
        trade["runner_peak_price"] = None
        trade["bracket_sizes"]["runner"] = runner_qty
        self.om.trades["TEST_001"] = trade
        return trade

    def test_price_activation_not_triggered_below_threshold(self):
        trade = self._tp2_trade()
        # tp2=105, r=2, ext=0.25 → activation at 105 + 0.5 = 105.5
        self.om._evaluate_runner(trade, 105.2)
        assert not trade["runner_activated"]

    def test_price_activation_triggered_at_threshold(self):
        trade = self._tp2_trade()
        self.om._evaluate_runner(trade, 105.5)
        assert trade["runner_activated"] is True
        assert trade["runner_peak_price"] == 105.5
        assert trade["state"] == TradeState.RUNNER_ACTIVE

    def test_price_activation_triggered_above_threshold(self):
        trade = self._tp2_trade()
        self.om._evaluate_runner(trade, 106.0)
        assert trade["runner_activated"] is True

    def test_time_activation_not_yet(self):
        om, broker = make_om()
        om._save_state = MagicMock()
        cfg = {**CONFIG, "runner": {"activation_type": "time", "time_delay_seconds": 60, "trailing_distance_r": 0.25}}
        om.config = cfg
        trade = make_trade()
        trade["state"] = TradeState.TP2_FILLED
        trade["runner_trailing_active"] = True
        trade["runner_activated"] = False
        trade["runner_tp2_fill_time"] = datetime.now().isoformat()
        trade["runner_peak_price"] = None
        om.trades["TEST_001"] = trade

        om._evaluate_runner(trade, 103.0)
        assert not trade["runner_activated"]

    def test_time_activation_elapsed(self):
        om, broker = make_om()
        om._save_state = MagicMock()
        cfg = {**CONFIG, "runner": {"activation_type": "time", "time_delay_seconds": 5, "trailing_distance_r": 0.25}}
        om.config = cfg
        trade = make_trade()
        trade["state"] = TradeState.TP2_FILLED
        trade["runner_trailing_active"] = True
        trade["runner_activated"] = False
        trade["runner_tp2_fill_time"] = (datetime.now() - timedelta(seconds=10)).isoformat()
        trade["runner_peak_price"] = None
        om.trades["TEST_001"] = trade

        om._evaluate_runner(trade, 103.0)
        assert trade["runner_activated"] is True

    def test_trailing_ratchet_moves_stop_on_new_peak(self):
        trade = self._tp2_trade()
        trade["runner_activated"] = True
        trade["runner_peak_price"] = 106.0
        trade["current_stop_price"] = 105.5  # 106 - 0.25*2

        mock_stop = MagicMock()
        mock_stop.order.orderId = 2
        self.broker.ib.openTrades.return_value = [mock_stop]

        # New high at 107 → new stop = 107 - 0.5 = 106.5
        self.om._evaluate_runner(trade, 107.0)
        assert trade["runner_peak_price"] == 107.0
        assert trade["current_stop_price"] == 106.5
        self.broker.ib.placeOrder.assert_called_once()

    def test_trailing_ratchet_does_not_move_stop_backward(self):
        trade = self._tp2_trade()
        trade["runner_activated"] = True
        trade["runner_peak_price"] = 107.0
        trade["current_stop_price"] = 106.5  # 107 - 0.5

        self.om._evaluate_runner(trade, 106.0)  # price retreats — no new peak
        assert trade["current_stop_price"] == 106.5
        self.broker.ib.placeOrder.assert_not_called()

    def test_short_trailing_ratchet_moves_stop_on_new_low(self):
        trade = self._tp2_trade(side="SELL")
        # SELL: entry=100, stop=102, r=2, tp2=95
        trade["runner_activated"] = True
        trade["runner_peak_price"] = 94.0
        trade["current_stop_price"] = 94.5  # 94 + 0.25*2 = 94.5

        mock_stop = MagicMock()
        mock_stop.order.orderId = 2
        self.broker.ib.openTrades.return_value = [mock_stop]

        # New low at 93 → new stop = 93 + 0.5 = 93.5 (< 94.5 → protective)
        self.om._evaluate_runner(trade, 93.0)
        assert trade["runner_peak_price"] == 93.0
        assert trade["current_stop_price"] == 93.5

    def test_short_ratchet_does_not_move_stop_backward(self):
        trade = self._tp2_trade(side="SELL")
        trade["runner_activated"] = True
        trade["runner_peak_price"] = 93.0
        trade["current_stop_price"] = 93.5  # 93 + 0.5

        self.om._evaluate_runner(trade, 94.0)  # price rises — not a new low
        assert trade["current_stop_price"] == 93.5
        self.broker.ib.placeOrder.assert_not_called()


# ---------------------------------------------------------------------------
# Scalp 0% runner — close after TP2
# ---------------------------------------------------------------------------

class TestScalpRunnerClose:
    def setup_method(self):
        self.om, self.broker = make_om()

    def test_scalp_closes_after_tp2(self):
        trade = make_trade(mode="scalp")
        trade["bracket_sizes"]["runner"] = 0
        trade["state"] = TradeState.TP2_FILLED
        trade["runner_trailing_active"] = False
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()

        mock_stop = MagicMock()
        mock_stop.order.orderId = 2
        self.broker.ib.openTrades.return_value = [mock_stop]

        self.om._activate_runner_trailing(trade)

        assert trade["state"] == TradeState.CLOSED
        assert trade["runner_trailing_active"] is True
        # Stop order was cancelled
        self.broker.ib.cancelOrder.assert_called_once()

    def test_scalp_does_not_start_runner_monitoring(self):
        trade = make_trade(mode="scalp")
        trade["bracket_sizes"]["runner"] = 0
        trade["state"] = TradeState.TP2_FILLED
        trade["runner_trailing_active"] = False
        self.om.trades["TEST_001"] = trade
        self.om._monitored_tickers["TEST_001"] = MagicMock()
        self.broker.ib.openTrades.return_value = []

        self.om._activate_runner_trailing(trade)

        assert "TEST_001" not in self.om._runner_tickers


# ---------------------------------------------------------------------------
# _get_check_price — price fallback chain
# ---------------------------------------------------------------------------

class TestGetCheckPrice:
    def setup_method(self):
        self.om, self.broker = make_om()

    def _ticker(self, bid=float("nan"), ask=float("nan"), last=float("nan")):
        t = MagicMock()
        t.bid = bid
        t.ask = ask
        t.last = last
        return t

    def _portfolio_item(self, symbol, price):
        item = MagicMock()
        item.contract.symbol = symbol
        item.marketPrice = price
        return item

    def test_buy_uses_bid(self):
        trade = make_trade(side="BUY")
        result = self.om._get_check_price(trade, self._ticker(bid=101.0, ask=101.5, last=100.9))
        assert result == 101.0

    def test_buy_falls_back_to_last_when_bid_nan(self):
        trade = make_trade(side="BUY")
        result = self.om._get_check_price(trade, self._ticker(ask=101.5, last=100.9))
        assert result == 100.9

    def test_buy_falls_back_to_portfolio_when_all_nan(self):
        trade = make_trade(side="BUY")
        self.broker.ib.portfolio.return_value = [self._portfolio_item("AAPL", 102.5)]
        result = self.om._get_check_price(trade, self._ticker())
        assert result == 102.5

    def test_returns_none_when_everything_invalid(self):
        trade = make_trade(side="BUY")
        self.broker.ib.portfolio.return_value = []
        result = self.om._get_check_price(trade, self._ticker())
        assert result is None

    def test_sell_uses_ask(self):
        trade = make_trade(side="SELL", entry=100.0, stop=102.0)
        result = self.om._get_check_price(trade, self._ticker(bid=99.0, ask=99.5, last=99.2))
        assert result == 99.5

    def test_sell_falls_back_to_last_when_ask_nan(self):
        trade = make_trade(side="SELL", entry=100.0, stop=102.0)
        result = self.om._get_check_price(trade, self._ticker(bid=99.0, last=99.2))
        assert result == 99.2

    def test_portfolio_wrong_symbol_ignored(self):
        trade = make_trade(side="BUY")
        self.broker.ib.portfolio.return_value = [self._portfolio_item("TSLA", 102.5)]
        result = self.om._get_check_price(trade, self._ticker())
        assert result is None

    def test_zero_price_treated_as_invalid(self):
        trade = make_trade(side="BUY")
        result = self.om._get_check_price(trade, self._ticker(bid=0.0, ask=0.0, last=0.0))
        # 0 is not > 0, so falls through to portfolio
        self.broker.ib.portfolio.return_value = [self._portfolio_item("AAPL", 102.0)]
        result = self.om._get_check_price(trade, self._ticker(bid=0.0))
        assert result == 102.0
