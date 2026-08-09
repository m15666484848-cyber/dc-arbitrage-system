"""风控服务:静默时段、仓位上限、并发上限、单日亏损熔断。"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import RiskConfig
from app.models.trading import Position, Trade
from app.models.customer import Customer


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_hhmm(s: str) -> time:
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]))


def is_in_silent_period(ranges: list[dict[str, Any]], now: datetime) -> bool:
    """判断当前时间是否在静默时段(支持跨午夜,如 23:00-07:00)。"""
    if not ranges:
        return False
    t = now.time()
    for r in ranges:
        start = _parse_hhmm(str(r.get("start", "00:00")))
        end = _parse_hhmm(str(r.get("end", "00:00")))
        if start <= end:
            if start <= t <= end:
                return True
        else:
            # 跨午夜
            if t >= start or t <= end:
                return True
    return False


async def get_risk_config(
    db: AsyncSession, customer_id: int, exchange: str
) -> RiskConfig | None:
    """获取风控配置(优先精确交易所,其次 all)。"""
    stmt = select(RiskConfig).where(
        RiskConfig.customer_id == customer_id,
        RiskConfig.enabled.is_(True),
    )
    result = await db.execute(stmt)
    configs = result.scalars().all()
    # 精确匹配优先
    for c in configs:
        if c.exchange == exchange:
            return c
    for c in configs:
        if c.exchange == "all":
            return c
    return None


async def check_order_amount(
    db: AsyncSession, customer_id: int, notional_usdt: float, exchange: str = "all"
) -> tuple[bool, str]:
    """校验单笔下单金额是否超过上限。

    优先级:Customer.max_order_usdt(管理员强制,默认 5000) > RiskConfig.max_position_usdt(客户自配)
    两者都为 0 表示不限。
    """
    if notional_usdt <= 0:
        return True, "ok"
    cust = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if not cust:
        return False, "客户不存在"
    # 管理员强制上限(默认 5000)
    if cust.max_order_usdt > 0 and notional_usdt > cust.max_order_usdt:
        return False, f"单笔下单金额 {notional_usdt:.2f} USDT 超过管理员上限 {cust.max_order_usdt:.2f} USDT"
    # 客户自配上限(更严格的优先)
    cfg = await get_risk_config(db, customer_id, exchange)
    if cfg and cfg.max_position_usdt > 0 and notional_usdt > cfg.max_position_usdt:
        return False, f"单笔下单金额 {notional_usdt:.2f} USDT 超过风控上限 {cfg.max_position_usdt:.2f} USDT"
    return True, "ok"


async def check_can_trade(
    db: AsyncSession, customer_id: int, exchange: str, symbol: str
) -> tuple[bool, str]:
    """下单前风控检查。返回 (是否允许, 原因)。

    含:客户激活、时间授权、静默时段、并发持仓数、单日亏损熔断。
    注意:金额上限由 check_order_amount 单独校验,因为它需要 notional_usdt 参数。
    """
    # 1. 客户激活状态
    cust = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()
    if not cust or not cust.is_active:
        return False, "客户未激活"
    # 2. 时间授权(授权服务单独校验,这里兜底)
    from app.services.authz import has_valid_authorization

    if not await has_valid_authorization(db, customer_id, exchange):
        return False, "未授权或授权已过期"
    # 3. 风控配置
    cfg = await get_risk_config(db, customer_id, exchange)
    now = _now()
    if cfg:
        if is_in_silent_period(cfg.silent_ranges or [], now):
            if cfg.silent_action == "ignore":
                return False, "当前为静默时段,信号忽略"
            # delay/log_only 允许记录但延迟;此处简化为允许(由上层处理延迟)
        # 4. 并发持仓数(只统计 master 仓位,parent_id IS NULL,避免子仓位重复计数)
        if cfg.max_concurrent_positions > 0:
            open_count = (
                await db.execute(
                    select(func.count(Position.id)).where(
                        Position.customer_id == customer_id,
                        Position.exchange == exchange,
                        Position.status == "open",
                        Position.parent_id.is_(None),  # 只查 master 仓位
                    )
                )
            ).scalar_one()
            if open_count >= cfg.max_concurrent_positions:
                return False, f"已达最大并发持仓数 {cfg.max_concurrent_positions}"
        # 5. 单日亏损
        if cfg.max_daily_loss_pct > 0:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_pnl = (
                await db.execute(
                    select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                        Trade.customer_id == customer_id,
                        Trade.exchange == exchange,
                        Trade.is_close.is_(True),
                        Trade.executed_at >= today_start,
                    )
                )
            ).scalar_one()
            # 取账户权益估算
            from app.models.config import EquitySnapshot

            last_eq = (
                await db.execute(
                    select(EquitySnapshot.equity)
                    .where(EquitySnapshot.customer_id == customer_id)
                    .order_by(EquitySnapshot.snapshot_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            base = last_eq or 1000.0
            if daily_pnl < 0 and abs(daily_pnl) / base * 100 >= cfg.max_daily_loss_pct:
                return False, f"触发单日最大亏损 {cfg.max_daily_loss_pct}%"
    return True, "ok"
