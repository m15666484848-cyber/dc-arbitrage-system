"""持仓管理:实时盈亏、止盈止损触发、成本保护触发、追踪止损、监控循环。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.trading import Position
from app.services import exchange_adapter, order_manager


def compute_pnl(position: Position, current_price: float) -> tuple[float, float]:
    """返回 (未实现盈亏 USDT, 盈亏比例%)。"""
    if not current_price or current_price <= 0:
        return 0.0, 0.0
    if position.side == "long":
        pnl = (current_price - position.entry_price) * position.qty
    else:
        pnl = (position.entry_price - current_price) * position.qty
    cost = position.entry_price * position.qty
    pct = (pnl / cost * 100) if cost > 0 else 0.0
    return pnl, pct


def should_trigger_tp(position: Position, current_price: float) -> int | None:
    """检查是否触及某级止盈,返回级别(1-based)或 None。"""
    for tp in (position.tp_levels or []):
        if tp.get("status") != "pending":
            continue
        price = float(tp.get("price", 0))
        if price <= 0:
            continue
        if position.side == "long" and current_price >= price:
            return int(tp.get("level", 0))
        if position.side == "short" and current_price <= price:
            return int(tp.get("level", 0))
    return None


def should_trigger_sl(position: Position, current_price: float) -> bool:
    if not position.sl or position.sl <= 0:
        return False
    if position.side == "long" and current_price <= position.sl:
        return True
    if position.side == "short" and current_price >= position.sl:
        return True
    return False


def should_trigger_cost_protection(position: Position, current_price: float) -> bool:
    """达到 TP1 已平仓后由 order_manager 触发;这里检测 +2% 利润触发成本保护。"""
    if position.breakeven_moved:
        return False
    _, pct = compute_pnl(position, current_price)
    return pct >= 2.0


async def enrich_position(position: Position, current_price: float, kol_name: str = "") -> dict:
    """填充实时字段用于 API 输出。"""
    pnl, pct = compute_pnl(position, current_price)
    return {
        "id": position.id,
        "customer_id": position.customer_id,
        "kol_id": position.kol_id,
        "kol_name": kol_name,
        "parent_id": position.parent_id,
        "exchange": position.exchange,
        "symbol": position.symbol,
        "side": position.side,
        "entry_price": position.entry_price,
        "qty": position.qty,
        "initial_qty": position.initial_qty,
        "tp_levels": position.tp_levels,
        "sl": position.sl,
        "leverage": position.leverage,
        "cost_protection": position.cost_protection,
        "breakeven_moved": position.breakeven_moved,
        "trailing_stop": position.trailing_stop,
        "status": position.status,
        "realized_pnl": position.realized_pnl,
        "current_price": current_price,
        "unrealized_pnl": pnl,
        "pnl_pct": pct,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
    }


async def _check_one_position(db: AsyncSession, position: Position) -> None:
    """检查单个持仓的止盈止损/成本保护/追踪止损触发(自行获取价格)。"""
    if position.status != "open" or position.qty <= 0:
        return
    current_price = await exchange_adapter.fetch_market_price(position.exchange, position.symbol)
    if not current_price or current_price <= 0:
        return
    await _check_one_position_with_price(db, position, current_price)


async def _check_one_position_with_price(db: AsyncSession, position: Position, current_price: float) -> None:
    """检查单个持仓的止盈止损/成本保护/追踪止损触发(使用已获取的价格)。"""
    if position.status != "open" or position.qty <= 0:
        return
    if not current_price or current_price <= 0:
        return

    # 止损触发 → 全部平仓
    if should_trigger_sl(position, current_price):
        logger.info(f"止损触发 pos={position.id} {position.symbol} price={current_price} sl={position.sl}")
        await order_manager.close_position(db, position.id, position.qty)
        return

    # 止盈触发 → 按比例平仓 + 成本保护
    tp_level = should_trigger_tp(position, current_price)
    if tp_level:
        logger.info(f"止盈{tp_level}触发 pos={position.id} {position.symbol} price={current_price}")
        await order_manager.close_at_tp_level(db, position, tp_level, current_price)
        return

    # 成本保护(+2% 利润,且 TP1 未触发时也保护)
    if should_trigger_cost_protection(position, current_price):
        await order_manager.apply_cost_protection(db, position)
        return

    # 追踪止损:盈利时按回撤比例动态上移止损
    if position.trailing_stop and position.trailing_callback > 0:
        await _update_trailing_stop(db, position, current_price)


async def _update_trailing_stop(db: AsyncSession, position: Position, current_price: float) -> None:
    pnl, _ = compute_pnl(position, current_price)
    if pnl <= 0:
        return
    callback = position.trailing_callback
    if position.side == "long":
        new_sl = current_price * (1 - callback)
        if not position.sl or new_sl > position.sl:
            position.sl = new_sl
            await db.commit()
    else:
        new_sl = current_price * (1 + callback)
        if not position.sl or new_sl < position.sl:
            position.sl = new_sl
            await db.commit()


async def monitor_loop() -> None:
    """后台持仓监控循环:每 5 秒检查所有 open 子仓位。

    只检查子仓位(parent_id IS NOT NULL),不检查 master 仓位。
    原因:master 和子仓位都有 tp_levels/sl 配置,如果同时检查会导致:
      1. master 触发止盈时走简单逻辑,close_position 会关闭所有子仓位(而非按比例)
      2. 子仓位触发止盈后 master 的 tp_levels 未更新,下次循环 master 会重复触发
    子仓位的 close_at_tp_level 聚合逻辑会正确同步 master 状态。

    优化:按 exchange 分组批量查询价格,减少 API 调用次数。
    """
    logger.info("持仓监控循环已启动")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Position).where(
                        Position.status == "open",
                        Position.parent_id.is_not(None),
                    )
                )
                positions = result.scalars().all()
                if not positions:
                    await asyncio.sleep(5)
                    continue

                exchange_symbols: dict[str, set[str]] = {}
                for pos in positions:
                    exchange_symbols.setdefault(pos.exchange, set()).add(pos.symbol)

                price_cache: dict[tuple[str, str], float] = {}
                for exh, syms in exchange_symbols.items():
                    prices = await exchange_adapter.fetch_market_prices_batch(exh, list(syms))
                    for sym, price in prices.items():
                        price_cache[(exh, sym)] = price

                for pos in positions:
                    try:
                        current_price = price_cache.get((pos.exchange, pos.symbol))
                        if not current_price or current_price <= 0:
                            continue
                        await _check_one_position_with_price(db, pos, current_price)
                    except Exception as e:
                        logger.exception(f"检查持仓 {pos.id} 失败: {e}")
        except Exception as e:
            logger.exception(f"持仓监控循环异常: {e}")
        await asyncio.sleep(5)
