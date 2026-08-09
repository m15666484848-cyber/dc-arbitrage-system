"""待触发限价单管理:创建、监控触发、取消、过期清理。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.pending_order import PendingOrder
from app.schemas.signal import ParsedSignal
from app.services import exchange_adapter, order_manager
from app.services.event_bus import bus
from app.services.notification import notify

# 入场价偏离市价超过此阈值(0.1%)时,创建待触发单而非直接市价下单
ENTRY_DEVIATION_THRESHOLD = 0.001

# 默认过期时间(7天)
DEFAULT_EXPIRE_DAYS = 7

# 每客户最多待触发单数(防滥用)
MAX_PENDING_PER_CUSTOMER = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def should_use_pending_order(entry_price: float, market_price: float, side: str) -> bool:
    """判断是否应该创建待触发单(入场价远离市价时)。

    long: 入场价低于市价 0.1% 以上 → 等价格跌到入场价
    short: 入场价高于市价 0.1% 以上 → 等价格涨到入场价
    """
    if not entry_price or not market_price or market_price <= 0:
        return False
    deviation = abs(entry_price - market_price) / market_price
    if deviation < ENTRY_DEVIATION_THRESHOLD:
        return False
    # long: 入场价应低于市价(低买);short: 入场价应高于市价(高卖)
    if side == "long" and entry_price < market_price:
        return True
    if side == "short" and entry_price > market_price:
        return True
    # 入场价方向反了(如 long 但入场价高于市价)→ 直接市价追单
    return False


def is_price_triggered(entry_price: float, current_price: float, side: str) -> bool:
    """检查当前价格是否触及入场价。"""
    if not current_price or current_price <= 0:
        return False
    if side == "long":
        # 多单:价格跌到入场价或更低时触发
        return current_price <= entry_price
    else:
        # 空单:价格涨到入场价或更高时触发
        return current_price >= entry_price


async def create_pending_order(
    db: AsyncSession,
    *,
    customer_id: int,
    kol_id: int,
    signal_id: int | None,
    exchange: str,
    parsed: ParsedSignal,
    notional_usdt: float,
    defaults: dict,
    strategy_id: int | None = None,
    expire_days: int = DEFAULT_EXPIRE_DAYS,
) -> dict:
    """创建待触发限价单。"""
    # 检查待触发单数量上限
    count = (
        await db.execute(
            select(func.count(PendingOrder.id)).where(
                PendingOrder.customer_id == customer_id,
                PendingOrder.status == "pending",
            )
        )
    ).scalar_one()
    if count >= MAX_PENDING_PER_CUSTOMER:
        return {"ok": False, "reason": f"待触发单已达上限 {MAX_PENDING_PER_CUSTOMER}"}

    # 检查同品种同方向是否已有 pending 单(避免重复)
    existing = (
        await db.execute(
            select(PendingOrder).where(
                PendingOrder.customer_id == customer_id,
                PendingOrder.exchange == exchange,
                PendingOrder.symbol == parsed.symbol,
                PendingOrder.side == parsed.side,
                PendingOrder.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing:
        return {"ok": False, "reason": f"已有 {parsed.symbol} {parsed.side} 的待触发单 (id={existing.id})"}

    # 构建止盈配置(基于入场价)
    tp_levels = order_manager._build_tp_levels(parsed, defaults, parsed.entry_price, parsed.side)

    pending = PendingOrder(
        customer_id=customer_id,
        kol_id=kol_id,
        signal_id=signal_id,
        exchange=exchange,
        symbol=parsed.symbol,
        side=parsed.side,
        entry_price=parsed.entry_price,
        notional_usdt=notional_usdt,
        leverage=parsed.leverage,
        tp_levels=tp_levels,
        sl=parsed.stop_loss,
        strategy_params={
            "defaults": defaults,
            "strategy_id": strategy_id,
        },
        status="pending",
        expires_at=_utcnow() + timedelta(days=expire_days),
    )
    db.add(pending)
    await db.commit()
    await db.refresh(pending)

    logger.info(
        f"创建待触发单 id={pending.id} customer={customer_id} "
        f"{parsed.symbol} {parsed.side} entry={parsed.entry_price} "
        f"expires={pending.expires_at}"
    )

    await bus.publish_customer(
        customer_id, "pending_order",
        {"id": pending.id, "symbol": parsed.symbol, "side": parsed.side,
         "entry_price": parsed.entry_price, "status": "pending"},
    )
    await notify(
        "pending", "待触发单已创建",
        f"品种: {parsed.symbol}\n方向: {parsed.side}\n目标入场价: {parsed.entry_price}\n"
        f"名义价值: {notional_usdt} USDT\n过期时间: {pending.expires_at}",
        customer_id,
    )

    return {"ok": True, "pending_id": pending.id}


async def trigger_pending_order(db: AsyncSession, pending: PendingOrder) -> dict:
    """触发待触发单:市价下单并更新状态。"""
    if pending.status != "pending":
        return {"ok": False, "reason": f"待触发单状态为 {pending.status},不可触发"}

    # 重新构建 ParsedSignal(从存储的数据恢复)
    parsed = ParsedSignal(
        symbol=pending.symbol,
        side=pending.side,
        entry_price=pending.entry_price,
        take_profits=[t["price"] for t in (pending.tp_levels or [])],
        stop_loss=pending.sl,
        leverage=pending.leverage,
    )

    strategy_params = pending.strategy_params or {}
    defaults = strategy_params.get("defaults", {})
    strategy_id = strategy_params.get("strategy_id")

    strategy = None
    if strategy_id:
        from app.models.strategy import Strategy
        strategy = await db.get(Strategy, strategy_id)

    # 获取交易所账号的 testnet 设置
    from app.models.config import ExchangeAccount
    ex_acc = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.customer_id == pending.customer_id,
                ExchangeAccount.exchange == pending.exchange,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalars().first()
    testnet = ex_acc.testnet if ex_acc else False

    # 调用 _place_entry 下单
    try:
        result = await order_manager._place_entry(
            db,
            customer_id=pending.customer_id,
            kol_id=pending.kol_id,
            signal_id=pending.signal_id,
            exchange=pending.exchange,
            testnet=testnet,
            parsed=parsed,
            notional_usdt=pending.notional_usdt,
            defaults=defaults,
            market_price=pending.entry_price,
            strategy=strategy,
        )
    except Exception as e:
        logger.exception(f"触发待触发单 {pending.id} 下单失败: {e}")
        await notify(
            "error", "待触发单下单失败",
            f"品种: {pending.symbol}\n方向: {pending.side}\n入场价: {pending.entry_price}\n错误: {e}",
            pending.customer_id,
        )
        return {"ok": False, "reason": f"下单异常: {e}"}

    # 更新待触发单状态
    pending.status = "triggered"
    pending.triggered_at = _utcnow()
    pending.triggered_order_id = result.get("order_id")
    pending.triggered_position_id = result.get("position_id")
    await db.commit()

    logger.info(
        f"待触发单 {pending.id} 已触发 order={result.get('order_id')} "
        f"position={result.get('position_id')}"
    )

    await bus.publish_customer(
        pending.customer_id, "pending_order",
        {"id": pending.id, "status": "triggered",
         "order_id": result.get("order_id"), "position_id": result.get("position_id")},
    )
    await notify(
        "order", "待触发单已成交",
        f"品种: {pending.symbol}\n方向: {pending.side}\n入场价: {pending.entry_price}\n"
        f"订单ID: {result.get('order_id')}",
        pending.customer_id,
    )

    return {"ok": True, "order_id": result.get("order_id"), "position_id": result.get("position_id")}


async def cancel_pending_order(db: AsyncSession, pending_id: int, customer_id: int, reason: str = "") -> dict:
    """手动取消待触发单。"""
    pending = (
        await db.execute(
            select(PendingOrder).where(
                PendingOrder.id == pending_id,
                PendingOrder.customer_id == customer_id,
            )
        )
    ).scalar_one_or_none()
    if not pending:
        return {"ok": False, "reason": "待触发单不存在"}
    if pending.status != "pending":
        return {"ok": False, "reason": f"待触发单状态为 {pending.status},不可取消"}

    pending.status = "cancelled"
    pending.cancel_reason = reason or "用户手动取消"
    await db.commit()

    await bus.publish_customer(
        customer_id, "pending_order",
        {"id": pending.id, "status": "cancelled"},
    )
    logger.info(f"待触发单 {pending.id} 已取消: {pending.cancel_reason}")
    return {"ok": True}


async def cleanup_expired_orders(db: AsyncSession) -> int:
    """清理过期的待触发单,返回清理数量。"""
    now = _utcnow()
    expired = (
        await db.execute(
            select(PendingOrder).where(
                PendingOrder.status == "pending",
                PendingOrder.expires_at < now,
            )
        )
    ).scalars().all()

    for pending in expired:
        pending.status = "expired"
        pending.cancel_reason = "已过期"
        await notify(
            "pending", "待触发单已过期",
            f"品种: {pending.symbol}\n方向: {pending.side}\n"
            f"目标入场价: {pending.entry_price}\n已自动取消",
            pending.customer_id,
        )

    if expired:
        await db.commit()
        logger.info(f"清理了 {len(expired)} 个过期待触发单")

    return len(expired)


async def monitor_loop() -> None:
    """后台监控循环:每 2 秒检查所有 pending 待触发单。"""
    logger.info("待触发单监控循环已启动")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # 1. 清理过期单
                await cleanup_expired_orders(db)

                # 2. 查询所有 pending 待触发单
                pendings = (
                    await db.execute(
                        select(PendingOrder).where(PendingOrder.status == "pending")
                    )
                ).scalars().all()

                if not pendings:
                    await asyncio.sleep(2)
                    continue

                # 3. 按交易所分组批量获取价格(减少重复查询)
                symbols_by_exchange: dict[str, set[str]] = {}
                for p in pendings:
                    symbols_by_exchange.setdefault(p.exchange, set()).add(p.symbol)

                prices: dict[tuple[str, str], float] = {}
                for exchange, symbols in symbols_by_exchange.items():
                    try:
                        batch = await exchange_adapter.fetch_market_prices_batch(exchange, list(symbols))
                        for sym, price in batch.items():
                            prices[(exchange, sym)] = price
                    except Exception as e:
                        logger.warning(f"批量获取价格失败 {exchange}: {e}")

                # 4. 检查每个待触发单是否触及入场价
                for pending in pendings:
                    key = (pending.exchange, pending.symbol)
                    current_price = prices.get(key)
                    if not current_price:
                        continue

                    if is_price_triggered(pending.entry_price, current_price, pending.side):
                        logger.info(
                            f"待触发单 {pending.id} 价格触及: "
                            f"{pending.symbol} {pending.side} "
                            f"entry={pending.entry_price} current={current_price}"
                        )
                        try:
                            await trigger_pending_order(db, pending)
                        except Exception as e:
                            logger.exception(f"触发待触发单 {pending.id} 失败: {e}")

        except Exception as e:
            logger.exception(f"待触发单监控循环异常: {e}")

        await asyncio.sleep(2)


async def list_pending_orders(
    db: AsyncSession, customer_id: int, status: str | None = None
) -> list[dict]:
    """查询客户的待触发单列表。"""
    stmt = select(PendingOrder).where(PendingOrder.customer_id == customer_id)
    if status:
        stmt = stmt.where(PendingOrder.status == status)
    stmt = stmt.order_by(PendingOrder.created_at.desc()).limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    from app.models.kol import Kol
    kol_ids = {p.kol_id for p in rows if p.kol_id}
    kols = {
        k.id: k.name
        for k in (
            await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))
        ).scalars().all()
    } if kol_ids else {}

    return [
        {
            "id": p.id,
            "kol_id": p.kol_id,
            "kol_name": kols.get(p.kol_id, ""),
            "exchange": p.exchange,
            "symbol": p.symbol,
            "side": p.side,
            "entry_price": p.entry_price,
            "notional_usdt": p.notional_usdt,
            "leverage": p.leverage,
            "sl": p.sl,
            "tp_levels": p.tp_levels,
            "status": p.status,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "triggered_at": p.triggered_at.isoformat() if p.triggered_at else None,
            "triggered_order_id": p.triggered_order_id,
            "triggered_position_id": p.triggered_position_id,
            "cancel_reason": p.cancel_reason,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in rows
    ]
