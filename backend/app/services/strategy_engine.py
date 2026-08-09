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

            "default_sl_pct": -0.05,

            "cost_protection_buffer": 0.02,

            "no_stop_loss": False,

            "tp_levels": [3, 5, 8],

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

        # 反马丁格尔连胜熔断:达到 max_rounds 轮连胜后停止加仓,防止过度暴露

        if strategy.martingale_round >= max_rounds:

            return StrategyDecision(allow=False, notional_usdt=0.0, reason=f"反马丁格尔连胜 {max_rounds} 轮熔断", params=p)

        if strategy.last_result == "win" and strategy.last_qty > 0:

            qty = strategy.last_qty * multiplier

        else:

            qty = base_qty

    else:

        qty = base_qty



    return StrategyDecision(allow=True, notional_usdt=qty, params=p)





async def record_trade_result(

    db: AsyncSession, strategy_id: int, won: bool, notional_usdt: float,
    break_even: bool = False
) -> None:

    """成交平仓后更新策略状态。



    注意:notional_usdt 为本笔平仓对应的下单名义价值(USDT),用于马丁格尔/反马丁格尔

    下一单的仓位计算。统一使用 USDT 金额,避免与币种数量单位混淆。

    不在此处 commit,由调用方统一提交事务,保证与平仓记录原子性。

    """

    # 行级锁,防止并发平仓(同一策略多笔同时结算)导致 martingale_round 计数错乱

    strategy = (await db.execute(

        select(Strategy).where(Strategy.id == strategy_id).with_for_update()

    )).scalar_one_or_none()

    if not strategy:

        return

    strategy.last_result = "win" if won else "loss"

    strategy.last_qty = float(notional_usdt or 0.0)

    if strategy.type == STRATEGY_MARTINGALE:

        if break_even:
            pass  # P2-5 fix: break-even does not count
        elif won:

            strategy.martingale_round = 0

        else:

            strategy.martingale_round += 1

    elif strategy.type == STRATEGY_ANTI_MARTINGALE:

        if break_even:
            pass  # P2-5 fix: break-even does not count
        elif not won:

            strategy.martingale_round = 0

            strategy.last_qty = 0.0

        else:

            strategy.martingale_round += 1





def _tp_levels_to_pct(tp_levels: list) -> list[float]:

    """将 tp_levels 配置转换为小数百分比列表。



    支持两种格式:

      简化格式: [10, 20, 30] -> [0.1, 0.2, 0.3]

      旧格式: [[0.10, 0.3], [0.20, 0.3]] -> [0.1, 0.2]

    """

    if not tp_levels:

        return [0.10, 0.20]

    result = []

    for v in tp_levels:

        if isinstance(v, (list, tuple)):

            v = float(v[0]) if len(v) >= 1 else 0.0

        else:

            v = float(v)

        if v >= 1.0:

            v = v / 100.0

        result.append(v)

    return result





def get_strategy_defaults(params: dict[str, Any]) -> dict:

    """从策略 params 提取信号兜底/止盈分级等配置。



    default_tp_pct 从 tp_levels 自动派生,不再作为独立策略参数。

    """

    tp_levels = params.get("tp_levels", [3, 5, 8])
    default_tp_pct = params.get("default_tp_pct")
    if default_tp_pct is None:
        default_tp_pct = _tp_levels_to_pct(tp_levels)

    return {

        "default_tp_pct": default_tp_pct,

        "default_sl_pct": params.get("default_sl_pct", -0.05),

        "no_stop_loss": params.get("no_stop_loss", False),

        "cost_protection_buffer": params.get("cost_protection_buffer", 0.002),

        "tp_levels": tp_levels,

        "enable_trailing": params.get("enable_trailing", False),

        "trailing_callback": params.get("trailing_callback", 0.01),

        "batch_entry_enabled": params.get("batch_entry_enabled", True),

        "batch_entry_window": params.get("batch_entry_window", 300),

    }

