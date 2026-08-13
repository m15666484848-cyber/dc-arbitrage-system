"""策略引擎:根据策略类型与历史结果计算下单量。



- normal(普通):固定基础仓位

- martingale(马丁格尔):上一单亏损 → 下单 ×倍数;盈利重置;连亏达上限熔断

- anti_martingale(反马丁格尔):上一单盈利 → 下单 ×倍数;亏损重置

普通/反马丁仍使用策略级状态；马丁策略按 KOL + BTC/ETH 独立追踪状态。

"""

from __future__ import annotations



from dataclasses import dataclass

from typing import Any



from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

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





_MARTINGALE_SUPPORTED_SYMBOLS = {"BTC/USDT", "ETH/USDT"}


def _normalize_martingale_symbol(symbol: str | None) -> str:
    """把 BTCUSDT / BTC/USDT 等格式统一为 BTC/USDT。"""
    raw = (symbol or "").upper().replace("-", "/").replace("_", "/").strip()
    if raw in ("BTCUSDT", "BTC/USDT", "BTC"):
        return "BTC/USDT"
    if raw in ("ETHUSDT", "ETH/USDT", "ETH"):
        return "ETH/USDT"
    return raw


def _martingale_state_key(kol_id: int | None, symbol: str | None) -> str | None:
    """马丁状态作用域:同一策略下按 KOL + BTC/ETH 隔离。"""
    norm_symbol = _normalize_martingale_symbol(symbol)
    if not kol_id or norm_symbol not in _MARTINGALE_SUPPORTED_SYMBOLS:
        return None
    return f"{kol_id}:{norm_symbol}"


def _get_scoped_martingale_state(strategy: Strategy, kol_id: int | None, symbol: str | None) -> dict[str, Any] | None:
    key = _martingale_state_key(kol_id, symbol)
    if not key:
        return None
    state_map = strategy.martingale_state or {}
    state = state_map.get(key) or {}
    return {
        "key": key,
        "symbol": _normalize_martingale_symbol(symbol),
        "round": int(state.get("round", 0) or 0),
        "last_result": str(state.get("last_result", "") or ""),
        "last_qty": float(state.get("last_qty", 0.0) or 0.0),
    }


def compute_decision(
    strategy: Strategy | None,
    kol_id: int | None = None,
    symbol: str | None = None,
) -> StrategyDecision:

    """根据策略当前状态计算本次下单决策。"""

    if strategy is None:

        # 默认普通策略

        return StrategyDecision(allow=True, notional_usdt=100.0, params={

            "default_sl_pct": None,  # None=tiered default

            "cost_protection_buffer": 0.02,

            "no_stop_loss": False,

            "tp_levels": None,  # None=tiered default

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

        scoped_state = _get_scoped_martingale_state(strategy, kol_id, symbol)

        # 马丁暂时只支持 KOL 维度下的 BTC/ETH。其它币不参与马丁,按基础下单量执行。
        # 未传 kol_id/symbol 的旧调用继续使用策略级状态,避免测试和手动场景退化。
        if scoped_state is None:
            if kol_id is None and symbol is None:
                scoped_state = {
                    "last_result": strategy.last_result,
                    "last_qty": strategy.last_qty,
                    "round": strategy.martingale_round,
                    "symbol": "strategy",
                }
            else:
                qty = base_qty

        if scoped_state is not None and scoped_state["round"] >= max_rounds:

            return StrategyDecision(
                allow=False,
                notional_usdt=0.0,
                reason=f"马丁格尔 {scoped_state['symbol']} 连亏 {max_rounds} 轮熔断",
                params=p,
            )

        elif scoped_state is not None and scoped_state["last_result"] == "loss" and scoped_state["last_qty"] > 0:

            qty = scoped_state["last_qty"] * multiplier

        else:

            qty = base_qty

    elif strategy.type == STRATEGY_ANTI_MARTINGALE:
        # 反马丁格尔连胜熔断:达到 max_rounds 轮连胜后停止加仓,防止过度暴露
        # L-6修复: 使用 scoped state 按 KOL+symbol 隔离,与马丁策略一致
        scoped_state = _get_scoped_martingale_state(strategy, kol_id, symbol)
        if scoped_state is None:
            if kol_id is None and symbol is None:
                scoped_state = {
                    "last_result": strategy.last_result,
                    "last_qty": strategy.last_qty,
                    "round": strategy.martingale_round,
                    "symbol": "strategy",
                }
            else:
                qty = base_qty
                scoped_state = None
        if scoped_state is not None and scoped_state["round"] >= max_rounds:
            return StrategyDecision(allow=False, notional_usdt=0.0, reason=f"反马丁格尔 {scoped_state['symbol']} 连胜 {max_rounds} 轮熔断", params=p)
        if scoped_state is not None and scoped_state["last_result"] == "win" and scoped_state["last_qty"] > 0:
            qty = scoped_state["last_qty"] * multiplier
        else:
            qty = base_qty

    else:

        qty = base_qty



    return StrategyDecision(allow=True, notional_usdt=qty, params=p)





async def record_trade_result(

    db: AsyncSession, strategy_id: int, won: bool, notional_usdt: float,
    break_even: bool = False,
    kol_id: int | None = None,
    symbol: str | None = None,
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

    if strategy.type == STRATEGY_MARTINGALE:

        scoped_state = _get_scoped_martingale_state(strategy, kol_id, symbol)

        # 马丁仅记录同一 KOL 的 BTC/ETH。其它币不更新马丁状态,避免串到后续 BTC/ETH。
        if scoped_state is None:
            return

        if break_even:
            return  # 保本不计入马丁轮次,也不改变上一单结果。

        state_map = dict(strategy.martingale_state or {})
        key = scoped_state["key"]

        if won:
            state_map[key] = {
                "round": 0,
                "last_result": "win",
                "last_qty": 0.0,
            }
        else:

            state_map[key] = {
                "round": scoped_state["round"] + 1,
                "last_result": "loss",
                "last_qty": float(notional_usdt or 0.0),
            }

        strategy.martingale_state = state_map
        flag_modified(strategy, "martingale_state")

    elif strategy.type == STRATEGY_ANTI_MARTINGALE:

        if break_even:
            pass  # P2-5 fix: break-even does not count
            # EX-M1 修复: 保本交易不影响连胜/连败计数,跳过 last_result 和 last_qty 的更新
        else:
            strategy.last_result = "win" if won else "loss"

            strategy.last_qty = float(notional_usdt or 0.0)

            if not won:

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
    use_tiered_defaults=True 时, 强制使用币种分层默认值(TIERED_CONFIG)。
    """
    # 分层模式: use_tiered_defaults=True 时, 强制 SL/TP 为 None, 由 signal_filter 按币种自动分配
    use_tiered = params.get("use_tiered_defaults", False)  # M-6修复: 默认改为False,尊重用户显式配置

    if use_tiered:
        # 分层模式: SL/TP 全部为 None, 由 signal_filter.apply_defaults 按币种自动分配
        tp_levels = None
        default_tp_pct = None
        default_sl_pct = None
    else:
        # 手动模式: 使用策略显式配置的 SL/TP
        tp_levels = params.get("tp_levels")  # None=按币种分层自动分配
        if tp_levels is None:
            default_tp_pct = None
        else:
            default_tp_pct = params.get("default_tp_pct")
            if default_tp_pct is None:
                default_tp_pct = _tp_levels_to_pct(tp_levels)
        default_sl_pct = params.get("default_sl_pct")  # None=按币种分层默认

    return {
        "default_tp_pct": default_tp_pct,
        "default_sl_pct": default_sl_pct,
        "no_stop_loss": params.get("no_stop_loss", False),
        "cost_protection_buffer": params.get("cost_protection_buffer", 0.02),
        "tp_levels": tp_levels,
        "enable_trailing": params.get("enable_trailing", False),
        "trailing_callback": params.get("trailing_callback", 0.01),
        "batch_entry_enabled": params.get("batch_entry_enabled", True),
        "batch_entry_window": params.get("batch_entry_window", 300),
        "max_sl_pct": params.get("max_sl_pct"),
        "timeout_protection_enabled": params.get("timeout_protection_enabled", True),
        "timeout_phase1_hours": params.get("timeout_phase1_hours", 4),
        "timeout_phase2_hours": params.get("timeout_phase2_hours", 24),
        "timeout_phase3_hours": params.get("timeout_phase3_hours", 72),
        "timeout_phase4_hours": params.get("timeout_phase4_hours", 96),
        "timeout_trailing_p1": params.get("timeout_trailing_p1", 0.03),
        "timeout_trailing_p2": params.get("timeout_trailing_p2", 0.02),
    }
