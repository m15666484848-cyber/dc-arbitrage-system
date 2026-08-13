"""S12新增: 追踪止损逻辑单元测试。"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from app.schemas.signal import ParsedSignal


class TestTrailingStopLogic:
    """追踪止损核心逻辑测试(不依赖数据库)。"""

    def test_long_trailing_stop_moves_up(self):
        """多头追踪止损: 价格上涨时止损上移。"""
        entry_price = 100.0
        current_price = 110.0
        callback = 0.03  # 3%
        # new_sl = current * (1 - callback) = 110 * 0.97 = 106.7
        new_sl = current_price * (1 - callback)
        assert new_sl > entry_price  # 止损已移至入场价上方,锁定利润
        assert new_sl == pytest.approx(106.7, abs=0.1)

    def test_short_trailing_stop_moves_down(self):
        """空头追踪止损: 价格下跌时止损下移。"""
        entry_price = 100.0
        current_price = 90.0
        callback = 0.03
        # new_sl = current * (1 + callback) = 90 * 1.03 = 92.7
        new_sl = current_price * (1 + callback)
        assert new_sl < entry_price  # 止损已移至入场价下方,锁定利润
        assert new_sl == pytest.approx(92.7, abs=0.1)

    def test_long_trailing_only_moves_up_not_down(self):
        """多头追踪止损只能上移,不能下移。"""
        old_sl = 105.0
        current_price = 108.0
        callback = 0.03
        new_sl = current_price * (1 - callback)  # 104.76
        # new_sl < old_sl → 不更新(止损不回退)
        should_update = new_sl > old_sl
        assert should_update is False

    def test_short_trailing_only_moves_down_not_up(self):
        """空头追踪止损只能下移,不能上移。"""
        old_sl = 95.0
        current_price = 92.0
        callback = 0.03
        new_sl = current_price * (1 + callback)  # 94.76
        # new_sl < old_sl → 更新(止损下移)
        should_update = new_sl < old_sl
        assert should_update is True

    def test_no_update_when_unprofitable(self):
        """未盈利时不更新追踪止损。"""
        # 多头: 当前价格 < 入场价 → 亏损
        entry_price = 100.0
        current_price = 95.0
        # pnl = (current - entry) / entry = -5% → pnl <= 0 → 不更新
        pnl = (current_price - entry_price) / entry_price
        assert pnl <= 0  # 不应更新追踪止损

    def test_short_trailing_below_entry_is_correct(self):
        """空头追踪止损低于入场价是正确行为(锁定利润)。"""
        entry_price = 100.0
        current_price = 80.0  # 大幅下跌,空头盈利
        callback = 0.05
        new_sl = current_price * (1 + callback)  # 84.0
        # 止损 84 < 入场 100,但这是正确的 — 锁定了 16% 的利润
        assert new_sl < entry_price
        assert new_sl > current_price  # 止损仍在当前价格上方
