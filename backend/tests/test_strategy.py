"""策略引擎单元测试(马丁格尔/反马丁格尔/熔断)。"""
from app.models.strategy import (
    STRATEGY_ANTI_MARTINGALE,
    STRATEGY_MARTINGALE,
    STRATEGY_NORMAL,
    Strategy,
)
from app.services.strategy_engine import compute_decision, get_strategy_defaults


def _strategy(type_: str, **kw) -> Strategy:
    base = dict(
        id=1, customer_id=1, name="t", type=type_,
        params={"base_qty": 100, "martingale_multiplier": 2, "max_rounds": 3},
        martingale_round=0, last_result="", last_qty=0.0, enabled=True,
    )
    base.update(kw)
    return Strategy(**base)


def test_normal_strategy_fixed_qty():
    s = _strategy(STRATEGY_NORMAL, last_result="loss", last_qty=999)
    d = compute_decision(s)
    assert d.allow
    assert d.notional_usdt == 100.0  # 普通策略永远基础仓位


def test_martingale_first_trade_base():
    s = _strategy(STRATEGY_MARTINGALE)
    d = compute_decision(s)
    assert d.notional_usdt == 100.0


def test_martingale_doubles_on_loss():
    s = _strategy(STRATEGY_MARTINGALE, last_result="loss", last_qty=100.0, martingale_round=1)
    d = compute_decision(s)
    assert d.notional_usdt == 200.0


def test_martingale_resets_on_win():
    s = _strategy(STRATEGY_MARTINGALE, last_result="win", last_qty=400.0, martingale_round=2)
    d = compute_decision(s)
    assert d.notional_usdt == 100.0  # 盈利重置


def test_martingale_circuit_breaker():
    s = _strategy(STRATEGY_MARTINGALE, last_result="loss", last_qty=400.0, martingale_round=3)
    d = compute_decision(s)
    assert not d.allow
    assert "熔断" in d.reason


def test_anti_martingale_doubles_on_win():
    s = _strategy(STRATEGY_ANTI_MARTINGALE, last_result="win", last_qty=100.0)
    d = compute_decision(s)
    assert d.notional_usdt == 200.0


def test_anti_martingale_resets_on_loss():
    s = _strategy(STRATEGY_ANTI_MARTINGALE, last_result="loss", last_qty=400.0)
    d = compute_decision(s)
    assert d.notional_usdt == 100.0


def test_default_strategy_when_none():
    d = compute_decision(None)
    assert d.allow
    assert d.notional_usdt == 100.0
    assert "tp_levels" in d.params


def test_get_strategy_defaults():
    p = {
        "default_tp_pct": [0.1, 0.2], "default_sl_pct": -0.05, "no_stop_loss": False,
        "cost_protection_buffer": 0.003, "tp_levels": [[0.1, 0.3]], "enable_trailing": True,
        "trailing_callback": 0.02, "batch_entry_enabled": True, "batch_entry_window": 300,
    }
    d = get_strategy_defaults(p)
    assert d["default_tp_pct"] == [0.1, 0.2]
    assert d["cost_protection_buffer"] == 0.003
    assert d["enable_trailing"] is True
