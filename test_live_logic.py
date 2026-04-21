"""
Live logic test for Phase 2A.2 — connects to TWS, validates all new features
without placing real orders.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("live_test")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append(condition)
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ------------------------------------------------------------------
# Load config (clientId 2 for test)
# ------------------------------------------------------------------
with open("config.json") as f:
    config = json.load(f)
config["ibkr"]["client_id"] = 2

from modules.broker import IBKRBroker
from modules.risk import RiskManager
from modules.order_manager import OrderManager, TradeState

broker = IBKRBroker(config)
rm = RiskManager(config)

print("\n=== Connecting to TWS ===")
connected = broker.connect()
check("TWS connection", connected)
if not connected:
    print("Cannot continue — not connected.")
    exit(1)

om = OrderManager(config, broker, rm)

# ------------------------------------------------------------------
# 1. Session detection
# ------------------------------------------------------------------
print("\n=== 1. Session Detection ===")
_ET = ZoneInfo("America/New_York")
session_state = om.get_session_state()
session_mode = om.get_session_mode()
check("get_session_state returns RTH or ETH", session_state in ("RTH", "ETH"), session_state)
check("default session_mode is 'auto'", session_mode == "auto", session_mode)

# Test _is_rth with fixed times
with patch("modules.order_manager.datetime") as mock_dt:
    mock_dt.now.return_value = datetime(2026, 4, 21, 10, 0, 0, tzinfo=_ET)  # Mon 10am
    check("_is_rth Mon 10am ET → True", om._is_rth() is True)

with patch("modules.order_manager.datetime") as mock_dt:
    mock_dt.now.return_value = datetime(2026, 4, 21, 7, 0, 0, tzinfo=_ET)  # Mon 7am
    check("_is_rth Mon 7am ET → False", om._is_rth() is False)

with patch("modules.order_manager.datetime") as mock_dt:
    mock_dt.now.return_value = datetime(2026, 4, 19, 10, 0, 0, tzinfo=_ET)  # Sat
    check("_is_rth Saturday → False", om._is_rth() is False)

# Session mode validation
try:
    om.set_session_mode("bad")
    check("set_session_mode invalid raises", False)
except ValueError:
    check("set_session_mode invalid raises", True)

om.set_session_mode("rth_only")
check("set_session_mode rth_only", om.get_session_mode() == "rth_only")
om.set_session_mode("auto")

# ------------------------------------------------------------------
# 2. Mode bracket calculations
# ------------------------------------------------------------------
print("\n=== 2. Execution Modes ===")
modes = config.get("modes", {})
check("config has conservative/gapper/scalp", set(modes.keys()) == {"conservative", "gapper", "scalp"})

for mode_name, mode_cfg in modes.items():
    total = mode_cfg["tp1_pct"] + mode_cfg["tp2_pct"] + mode_cfg["runner_pct"]
    check(f"{mode_name} percentages sum to 1.0", abs(total - 1.0) < 0.001, f"{total:.2f}")

# Conservative: 100 shares → 50/40/10
b = rm.calculate_bracket_sizes(100, modes["conservative"])
check("conservative 100sh → tp1=50 tp2=40 runner=10", b == {"tp1": 50, "tp2": 40, "runner": 10, "total": 100}, str(b))

# Gapper: 100 shares → 20/25/55
b = rm.calculate_bracket_sizes(100, modes["gapper"])
check("gapper 100sh → tp1=20 tp2=25 runner=55", b == {"tp1": 20, "tp2": 25, "runner": 55, "total": 100}, str(b))

# Scalp: 100 shares → 75/25/0
b = rm.calculate_bracket_sizes(100, modes["scalp"])
check("scalp 100sh → tp1=75 tp2=25 runner=0", b == {"tp1": 75, "tp2": 25, "runner": 0, "total": 100}, str(b))
check("scalp runner=0 allowed (no raise)", b["runner"] == 0)

# ------------------------------------------------------------------
# 3. Live price via portfolio fallback
# ------------------------------------------------------------------
print("\n=== 3. Live Price / Portfolio Fallback ===")
portfolio = broker.ib.portfolio()
check("portfolio() returns items", len(portfolio) > 0, f"{len(portfolio)} positions")
for item in portfolio:
    sym = item.contract.symbol
    price = item.marketPrice
    check(f"{sym} portfolio price is valid (>0)", price and price == price and price > 0, f"${price:.2f}")

# Test _get_check_price portfolio fallback with NaN ticker
ticker_mock = MagicMock()
ticker_mock.bid = float("nan")
ticker_mock.ask = float("nan")
ticker_mock.last = float("nan")

if portfolio:
    first = portfolio[0]
    sym = first.contract.symbol
    side = "SELL" if first.position < 0 else "BUY"
    dummy_trade = {
        "trade_id": "TEST_LIVE",
        "symbol": sym,
        "side": side,
        "entry_price": first.averageCost,
        "stop_price": first.averageCost * (1.02 if side == "SELL" else 0.98),
    }
    price = om._get_check_price(dummy_trade, ticker_mock)
    check(f"portfolio fallback returns {sym} price", price is not None and price > 0, f"${price:.2f}" if price else "None")

# ------------------------------------------------------------------
# 4. 1R protection logic
# ------------------------------------------------------------------
print("\n=== 4. 1R Protection Logic ===")

def dummy_trade(side="SELL", entry=370.0, stop=380.0):
    r = abs(entry - stop)
    return {
        "trade_id": "TEST_1R",
        "symbol": "TSLA",
        "side": side,
        "exit_side": "BUY" if side == "SELL" else "SELL",
        "entry_price": entry,
        "stop_price": stop,
        "current_stop_price": stop,
        "order_ids": {"stop": 999},
        "protection_1r_triggered": False,
        "updated_at": datetime.now().isoformat(),
        "tp_prices": {
            "tp1": round(entry - r * 1.5, 2) if side == "SELL" else round(entry + r * 1.5, 2),
            "tp2": round(entry - r * 2.5, 2) if side == "SELL" else round(entry + r * 2.5, 2),
        }
    }

om._save_state = MagicMock()

# SELL trade: entry=370, stop=380, r=10, trigger=360, new_stop=369.0
trade = dummy_trade(side="SELL", entry=370.0, stop=380.0)
om.trades["TEST_1R"] = trade
om._monitored_tickers["TEST_1R"] = MagicMock()

mock_stop = MagicMock()
mock_stop.order.orderId = 999  # matches trade["order_ids"]["stop"]

with patch.object(broker.ib, "openTrades", return_value=[]):
    om._evaluate_1r(trade, 365.0)  # above trigger → no trigger
    check("SELL 1R: price above trigger → no trigger", not trade["protection_1r_triggered"], "ask=365 trigger=360")

with patch.object(broker.ib, "openTrades", return_value=[mock_stop]), \
     patch.object(broker.ib, "placeOrder", return_value=MagicMock()):
    om._evaluate_1r(trade, 360.0)  # at trigger → fires
    check("SELL 1R: price at trigger → triggered", trade["protection_1r_triggered"], "ask=360")
    expected_stop = round(370.0 - 10.0 * config["protection"]["one_r_stop_offset_r"], 2)
    check("SELL 1R: new_stop computed correctly", trade["current_stop_price"] == expected_stop,
          f"expected {expected_stop} got {trade['current_stop_price']}")

    # BUY trade: entry=100, stop=98, r=2, trigger=102
    trade2 = dummy_trade(side="BUY", entry=100.0, stop=98.0)
    om.trades["TEST_1R_BUY"] = trade2
    om._monitored_tickers["TEST_1R_BUY"] = MagicMock()

    om._evaluate_1r(trade2, 101.5)  # below trigger
    check("BUY 1R: price below trigger → no trigger", not trade2["protection_1r_triggered"])
    om._evaluate_1r(trade2, 102.0)  # at trigger
    check("BUY 1R: price at trigger → triggered", trade2["protection_1r_triggered"])

# ------------------------------------------------------------------
# 5. Runner activation and trailing
# ------------------------------------------------------------------
print("\n=== 5. Runner Logic ===")

def runner_trade(side="BUY", entry=100.0, stop=98.0, runner_qty=5):
    r = abs(entry - stop)
    tp2 = round(entry + r * 2.5, 2) if side == "BUY" else round(entry - r * 2.5, 2)
    return {
        "trade_id": "TEST_RUNNER",
        "symbol": "TSLA",
        "side": side,
        "exit_side": "SELL" if side == "BUY" else "BUY",
        "entry_price": entry,
        "stop_price": stop,
        "current_stop_price": stop,
        "order_ids": {"stop": 888},
        "bracket_sizes": {"tp1": 25, "tp2": 20, "runner": runner_qty, "total": 50},
        "tp_prices": {
            "tp1": round(entry + r * 1.5, 2) if side == "BUY" else round(entry - r * 1.5, 2),
            "tp2": tp2,
        },
        "state": TradeState.TP2_FILLED,
        "runner_trailing_active": True,
        "runner_activated": False,
        "runner_peak_price": None,
        "runner_tp2_fill_time": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

runner_cfg = config["runner"]
# BUY: entry=100, stop=98, r=2, tp2=105, activation = 105 + 0.25*2 = 105.5
rt = runner_trade(side="BUY", entry=100.0, stop=98.0, runner_qty=5)
om.trades["TEST_RUNNER"] = rt
om._runner_tickers["TEST_RUNNER"] = MagicMock()

om._evaluate_runner(rt, 105.2)  # below activation
check("Runner: price below activation → not activated", not rt["runner_activated"], "price=105.2 threshold=105.5")

om._evaluate_runner(rt, 105.5)  # at activation
check("Runner: price at activation → activated", rt["runner_activated"], f"activation_type={runner_cfg['activation_type']}")
check("Runner: state → RUNNER_ACTIVE", rt["state"] == TradeState.RUNNER_ACTIVE)

# Ratchet: set peak, verify stop moves on new high, doesn't move on pullback
rt["runner_peak_price"] = 106.0
rt["current_stop_price"] = 105.5  # 106 - 0.25*2

with patch.object(broker.ib, "openTrades", return_value=[]), \
     patch.object(broker.ib, "placeOrder", return_value=MagicMock()):
    om._evaluate_runner(rt, 105.5)  # no new peak → stop stays
check("Runner ratchet: pullback → stop unchanged", rt["current_stop_price"] == 105.5)

# Time-based activation
print("\n  Time activation:")
cfg2 = dict(config)
cfg2["runner"] = {"activation_type": "time", "time_delay_seconds": 5, "trailing_distance_r": 0.25}
om2 = OrderManager(cfg2, broker, rm)
om2._save_state = MagicMock()

rt2 = runner_trade(side="BUY", entry=100.0, stop=98.0)
rt2["runner_tp2_fill_time"] = (datetime.now() - timedelta(seconds=10)).isoformat()
om2.trades["RT2"] = rt2
om2._runner_tickers["RT2"] = MagicMock()

om2._evaluate_runner(rt2, 103.0)
check("Runner time: 10s elapsed vs 5s delay → activated", rt2["runner_activated"])

rt3 = runner_trade(side="BUY", entry=100.0, stop=98.0)
rt3["runner_tp2_fill_time"] = datetime.now().isoformat()
cfg3 = dict(config)
cfg3["runner"] = {"activation_type": "time", "time_delay_seconds": 60, "trailing_distance_r": 0.25}
om3 = OrderManager(cfg3, broker, rm)
om3._save_state = MagicMock()
om3.trades["RT3"] = rt3
om3._runner_tickers["RT3"] = MagicMock()

om3._evaluate_runner(rt3, 103.0)
check("Runner time: 0s elapsed vs 60s delay → not activated", not rt3["runner_activated"])

# Scalp: runner=0 → closes after TP2
print("\n  Scalp (0% runner):")
scalp_t = runner_trade(side="BUY", runner_qty=0)
scalp_t["runner_trailing_active"] = False
scalp_t["state"] = TradeState.TP2_FILLED
om.trades["SCALP_T"] = scalp_t
om._monitored_tickers["SCALP_T"] = MagicMock()

with patch.object(broker.ib, "openTrades", return_value=[]), \
     patch.object(broker.ib, "cancelOrder", return_value=None):
    om._activate_runner_trailing(scalp_t)
check("Scalp: trade closes after TP2 (runner=0)", scalp_t["state"] == TradeState.CLOSED)
check("Scalp: runner monitoring NOT started", "SCALP_T" not in om._runner_tickers)

# ------------------------------------------------------------------
# 6. entry_outside_rth flag
# ------------------------------------------------------------------
print("\n=== 6. outsideRth Entry Flag ===")
with patch.object(OrderManager, "_is_rth", return_value=True):
    check("eth_allowed + RTH → outsideRth=True", om._entry_outside_rth("eth_allowed") is True)
    check("auto + RTH → outsideRth=False", om._entry_outside_rth("auto") is False)
    check("rth_only + RTH → outsideRth=False", om._entry_outside_rth("rth_only") is False)

with patch.object(OrderManager, "_is_rth", return_value=False):
    check("auto + ETH → outsideRth=True", om._entry_outside_rth("auto") is True)
    check("eth_allowed + ETH → outsideRth=True", om._entry_outside_rth("eth_allowed") is True)

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
broker.disconnect()

total = len(results)
passed = sum(results)
failed = total - passed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed", end="")
if failed:
    print(f"  (\033[91m{failed} FAILED\033[0m)")
else:
    print(f"  (\033[92mALL PASS\033[0m)")
print('='*50)
