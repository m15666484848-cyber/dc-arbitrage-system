"""S12新增: 信号纠错回写单元测试。"""
import pytest
from app.schemas.signal import ParsedSignal
from app.services.signal_filter import correct_direction, correct_price, apply_defaults


class TestCorrectDirection:
    """方向纠错测试。"""

    def test_long_sl_above_entry_flips_to_short(self):
        """long 止损高于入场 → 翻转为 short。"""
        parsed = ParsedSignal(
            raw_text="做多 BTC 止损 45000",
            symbol="BTC/USDT",
            side="long",
            entry_price=40000,
            stop_loss=45000,
            take_profits=[42000, 44000],
        )
        corrected, log = correct_direction(parsed)
        assert corrected is True
        assert parsed.side == "short"
        assert "翻转为 short" in log

    def test_short_sl_below_entry_flips_to_long(self):
        """short 止损低于入场 → 翻转为 long。"""
        parsed = ParsedSignal(
            raw_text="做空 BTC 止损 35000",
            symbol="BTC/USDT",
            side="short",
            entry_price=40000,
            stop_loss=35000,
            take_profits=[38000, 36000],
        )
        corrected, log = correct_direction(parsed)
        assert corrected is True
        assert parsed.side == "long"
        assert "翻转为 long" in log

    def test_correct_direction_no_change_when_valid(self):
        """方向正确时不纠错。"""
        parsed = ParsedSignal(
            raw_text="做多 BTC 止损 38000",
            symbol="BTC/USDT",
            side="long",
            entry_price=40000,
            stop_loss=38000,
            take_profits=[42000, 44000],
        )
        corrected, log = correct_direction(parsed)
        assert corrected is False
        assert log == ""

    def test_no_side_no_correction(self):
        """无方向不纠错。"""
        parsed = ParsedSignal(symbol="BTC/USDT", entry_price=40000)
        corrected, log = correct_direction(parsed)
        assert corrected is False


class TestCorrectPrice:
    """价格纠错测试。"""

    def test_price_within_tolerance_no_change(self):
        """价格偏差在容忍范围内不纠错。"""
        parsed = ParsedSignal(
            raw_text="做多 BTC 40000",
            symbol="BTC/USDT",
            side="long",
            entry_price=40000,
        )
        corrected, log, rejected = correct_price(parsed, market_price=40500)
        assert corrected is False
        assert rejected is False

    def test_price_large_deviation_rejected(self):
        """价格偏差超过30%被拒绝。"""
        parsed = ParsedSignal(
            raw_text="做多 BTC 60000",
            symbol="BTC/USDT",
            side="long",
            entry_price=60000,
        )
        corrected, log, rejected = correct_price(parsed, market_price=40000)
        assert rejected is True
        assert "偏离市价" in log

    def test_price_moderate_deviation_corrected(self):
        """价格偏差15%-30%自动纠正。"""
        parsed = ParsedSignal(
            raw_text="做多 BTC 50000",
            symbol="BTC/USDT",
            side="long",
            entry_price=50000,
        )
        corrected, log, rejected = correct_price(parsed, market_price=40000)
        assert corrected is True
        assert rejected is False
        assert parsed.entry_price == 40000

    def test_no_market_price_no_correction(self):
        """无市价不纠错。"""
        parsed = ParsedSignal(entry_price=40000)
        corrected, log, rejected = correct_price(parsed, market_price=None)
        assert corrected is False
        assert rejected is False
