"""策略引擎:根据策略类型与历史结果计算下单量。

- normal(普通):固定基础仓位
- martingale(马丁格尔):上一单亏损 → 下单 ×倍数;盈利重置;连亏达上限熔断
- anti_martingale(反马丁格尔):上一单盈利 → 下单 ×倍数;亏损重置
每策略独立追踪 martingale_round / last_result / last_qty。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import (
    STRATEGY_ANTI_MARTINGALE,
    STRATEGY_MARTINGALE,
    STRATEGY_NORMAL,
    Strategy,
)


@dataclass
class StrategyDecision:
    allow: bool
    notional_usdt: float  # 下单名义价值(USDT)
    reason: str = ""
    params: dict[str, Any] = None

    def __post_init__(self):
        if self.params is None:
            self.params = {}


async def get_strategy_for_follow(
    db: AsyncSession, customer_id: int, kol_id: int
) -> tuple[Strategy | None, float | None]:
    """获取客户关注某 KOL 时绑定的策略和跟单金额。

    Returns (strategy, notional_usdt_override):
    - strategy: 绑定的策略(无则 None 用默认)
    - notional_usdt_override: 客户自定义跟单金额(None 或 0 时使用策略中的 base_qty)
    """
    from app.models.kol import KolFollow

    stmt = select(KolFollow).where(
        KolFollow.customer_id == customer_id,
        KolFollow.kol_id == kol_id,
        KolFollow.enabled.is_(True),
    )
    follow = (await db.execute(stmt)).scalar_one_or_none()
    strategy = None
    notional_usdt = None
    if follow:
        notional_usdt = follow.followed_notional_usdt
        if follow.strategy_id:
            strategy = (await db.execute(select(Strategy).where(Strategy.id == follow.strategy_id))).scalar_one_or_none()
    return strategy, notional_usdt


def compute_decision(strategy: Strategy | None) -> StrategyDecision:
    """根据策略当前状态计算本次下单决策。"""
    if strategy is None:
        # 默认普通策略
        return StrategyDecision(allow=True, notional_usdt=100.0, params={
            "default_tp_pct": [0.10, 0.20],
            "default_sl_pct": -0.05,
            "cost_protection_buffer": 0.002,
            "no_stop_loss": False,
            "tp_levels": [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]],
            "enable_trailing": False,
            "trailing_callback": 0.01,
            "batch_entry_enabled": True,
        })

    p = strategy.params or {}
    base_qty = float(p.get("base_qty", 100.0))
    multiplier = float(p.get("martingale_multiplier", 2.0))
    max_rounds = int(p.get("max_rounds", 3))

    if strategy.type == STRATEGY_NORMAL:
        qty = base_qty
    elif strategy.type == STRATEGY_MARTINGALE:
        if strategy.martingale_round >= max_rounds:
            return StrategyDecision(allow=False, notional_usdt=0.0, reason=f"马丁格尔连亏 {max_rounds} 轮熔断", params=p)
        if strategy.last_result == "loss" and strategy.last_qty > 0:
            qty = strategy.last_qty * multiplier
        else:
            qty = base_qty
    elif strategy.type == STRATEGY_ANTI_MARTINGALE:
        if strategy.last_result == "win" and strategy.last_qty > 0:
            qty = strategy.last_qty * multiplier
        else:
            qty = base_qty
    else:
        qty = base_qty

    return StrategyDecision(allow=True, notional_usdt=qty, params=p)


async def record_trade_result(
    db: AsyncSession, strategy_id: int, won: bool, qty: float
) -> None:
    """成交平仓后更新策略状态。"""
    strategy = (await db.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one_or_none()
    if not strategy:
        return
    strategy.last_result = "win" if won else "loss"
    strategy.last_qty = qty
    if strategy.type == STRATEGY_MARTINGALE:
        if won:
            strategy.martingale_round = 0
        else:
            strategy.martingale_round += 1
    elif strategy.type == STRATEGY_ANTI_MARTINGALE:
        if not won:
            strategy.martingale_round = 0
            strategy.last_qty = 0.0
    await db.commit()


def get_strategy_defaults(params: dict[str, Any]) -> dict:
    """从策略 params 提取信号兜底/止盈分级等配置。"""
    return {
        "default_tp_pct": params.get("default_tp_pct", [0.10, 0.20]),
        "default_sl_pct": params.get("default_sl_pct", -0.05),
        "no_stop_loss": params.get("no_stop_loss", False),
        "cost_protection_buffer": params.get("cost_protection_buffer", 0.002),
        "tp_levels": params.get("tp_levels", [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]]),
        "enable_trailing": params.get("enable_trailing", False),
        "trailing_callback": params.get("trailing_callback", 0.01),
        "batch_entry_enabled": params.get("batch_entry_enabled", True),
        "batch_entry_window": params.get("batch_entry_window", 300),
    }
