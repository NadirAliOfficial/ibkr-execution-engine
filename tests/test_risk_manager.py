import pytest
from modules.risk import RiskManager

CONFIG = {
    "defaults": {"trailing_stop_pct": 0.02},
    "modes": {
        "conservative": {"tp1_pct": 0.50, "tp2_pct": 0.40, "runner_pct": 0.10},
        "gapper":       {"tp1_pct": 0.20, "tp2_pct": 0.25, "runner_pct": 0.55},
        "scalp":        {"tp1_pct": 0.75, "tp2_pct": 0.25, "runner_pct": 0.00},
    },
}


@pytest.fixture
def rm():
    return RiskManager(CONFIG)


class TestPositionSize:
    def test_basic(self, rm):
        assert rm.calculate_position_size(100.0, 98.0, 100.0) == 50

    def test_floors_fractional(self, rm):
        assert rm.calculate_position_size(100.0, 98.5, 100.0) == 66

    def test_same_price_raises(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(100.0, 100.0, 100.0)

    def test_too_small_raises(self, rm):
        with pytest.raises(ValueError):
            rm.calculate_position_size(100.0, 99.99, 0.001)


class TestBracketSizes:
    def test_conservative(self, rm):
        result = rm.calculate_bracket_sizes(100, CONFIG["modes"]["conservative"])
        assert result == {"tp1": 50, "tp2": 40, "runner": 10, "total": 100}

    def test_gapper(self, rm):
        result = rm.calculate_bracket_sizes(100, CONFIG["modes"]["gapper"])
        assert result == {"tp1": 20, "tp2": 25, "runner": 55, "total": 100}

    def test_scalp_zero_runner_allowed(self, rm):
        result = rm.calculate_bracket_sizes(100, CONFIG["modes"]["scalp"])
        assert result["runner"] == 0
        assert result["tp1"] == 75
        assert result["tp2"] == 25

    def test_runner_residual_assigned_correctly(self, rm):
        # 10 shares conservative: tp1=5, tp2=4, runner=1 (residual)
        result = rm.calculate_bracket_sizes(10, CONFIG["modes"]["conservative"])
        assert result["tp1"] + result["tp2"] + result["runner"] == 10

    def test_invalid_percentages_raises(self, rm):
        bad = {"tp1_pct": 0.50, "tp2_pct": 0.40, "runner_pct": 0.20}
        with pytest.raises(ValueError, match="sum to 100%"):
            rm.calculate_bracket_sizes(100, bad)

    def test_too_small_for_tp1_raises(self, rm):
        # 3 shares gapper: tp1=floor(0.6)=0 → raises
        with pytest.raises(ValueError):
            rm.calculate_bracket_sizes(3, CONFIG["modes"]["gapper"])

    def test_runner_nonzero_pct_but_zero_qty_raises(self, rm):
        # 2 shares conservative: tp1=1, tp2=0 → raises at tp2
        with pytest.raises(ValueError):
            rm.calculate_bracket_sizes(2, CONFIG["modes"]["conservative"])

    def test_no_mode_cfg_uses_defaults(self, rm):
        # defaults has no tp1_pct etc, so falls back to hardcoded 0.50/0.40/0.10
        result = rm.calculate_bracket_sizes(100, None)
        assert result["tp1"] == 50
        assert result["tp2"] == 40
        assert result["runner"] == 10


class TestTpPrices:
    def test_buy(self, rm):
        prices = rm.calculate_tp_prices(100.0, 98.0, "BUY")
        assert prices["tp1"] == 103.0   # 100 + 2*1.5
        assert prices["tp2"] == 105.0   # 100 + 2*2.5

    def test_sell(self, rm):
        prices = rm.calculate_tp_prices(100.0, 102.0, "SELL")
        assert prices["tp1"] == 97.0    # 100 - 2*1.5
        assert prices["tp2"] == 95.0    # 100 - 2*2.5

    def test_rounding(self, rm):
        prices = rm.calculate_tp_prices(100.0, 98.33, "BUY")
        r = abs(100.0 - 98.33)
        assert prices["tp1"] == round(100.0 + r * 1.5, 2)
        assert prices["tp2"] == round(100.0 + r * 2.5, 2)
