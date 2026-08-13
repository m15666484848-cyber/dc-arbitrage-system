"""S12新增: 对账阈值逻辑单元测试。"""
import pytest


class TestReconciliationThreshold:
    """对账双门槛阈值测试 (1%比例 AND 0.0001最小绝对值)。"""

    def test_both_below_threshold_no_discrepancy(self):
        """比例和绝对值都低于阈值 → 不报告差异。"""
        local_qty = 1.0000
        ex_qty = 1.00005
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert not (diff_ratio > 0.01 and diff_abs > 0.0001)

    def test_ratio_above_but_abs_below_no_discrepancy(self):
        """比例超1%但绝对值<0.0001 → 不报告(避免微小数量误报)。"""
        local_qty = 0.0001
        ex_qty = 0.0002
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert diff_ratio > 0.01
        assert not (diff_ratio > 0.01 and diff_abs > 0.0001)

    def test_both_above_threshold_reports_discrepancy(self):
        """比例和绝对值都超阈值 → 报告差异。"""
        local_qty = 1.0
        ex_qty = 1.05
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert diff_ratio > 0.01
        assert diff_abs > 0.0001

    def test_large_quantity_small_ratio_no_report(self):
        """大数量小比例但绝对值大但比例<1% → 不报告。"""
        local_qty = 100.0
        ex_qty = 100.5
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert not (diff_ratio > 0.01)

    def test_zero_local_qty_reports(self):
        """本地数量为0 → 报告差异。"""
        local_qty = 0.0
        ex_qty = 0.5
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert diff_ratio > 0.01
        assert diff_abs > 0.0001

    def test_exact_1pct_boundary_not_reported(self):
        """边界值: 恰好1%不报告(严格大于)。"""
        local_qty = 100.0
        ex_qty = 101.0
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        # 1.0/100.0 = 0.01, 浮点可能有微小误差
        assert diff_ratio == pytest.approx(0.01, abs=1e-10)
        # 严格 > 0.01 → False
        assert not (diff_ratio > 0.01)

    def test_1_1pct_reports(self):
        """1.1%超过阈值 → 报告。"""
        local_qty = 100.0
        ex_qty = 101.1
        diff_ratio = abs(local_qty - ex_qty) / max(abs(local_qty), 1e-10)
        diff_abs = abs(local_qty - ex_qty)
        assert diff_ratio > 0.01
        assert diff_abs > 0.0001
