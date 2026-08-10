"""持仓管理:实时盈亏、止盈止损触发、成本保护触发、追踪止损、监控循环。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.core.config import settings as _cfg
from app.models.config import RiskConfig
from app.models.trading import Position
from app.services import exchange_adapter, order_manager, price_feed
from app.services.event_bus import bus

# 内存锁:记录正在平仓中的仓位ID,防止止损监控循环重复触发同一仓位
# 作为 Redis 不可用时的进程内兜底
_closing_positions: set[int] = set()

DEFAULT_POSITION_TIMEOUT_HOURS = 48
# P3-6 修复: 缓存TTL与监控循环间隔(5秒)匹配,减少冗余API调用
PRICE_CACHE_TTL_SECONDS = 5
# P2 修复: 默认 TAKER 手续费率(0.1%)。
# 实际费率应从交易所 API 获取或通过配置覆盖,此处为估算值。
# 可通过环境变量 DEFAULT_TAKER_FEE_RATE 覆盖。
DEFAULT_TAKER_FEE_RATE = getattr(_cfg, 'default_taker_fee_rate', 0.001)
# 交易手续费率(可后续从交易所配置读取)
CLOSE_FEE_RATE = DEFAULT_TAKER_FEE_RATE  # 0.1% default, OKX taker fee


async def _add_closing_position(position_id: int) -> bool:
    """尝试获取平仓锁(跨进程,基于 Redis SET NX EX)。

    Returns: True 表示获取成功,False 表示已被其他进程/协程持有。
    Redis 不可用时回退到内存 set(仅单进程有效)。
    """
    try:
        redis = await get_redis()
        if redis:
            key = f"closing_pos:{position_id}"
            acquired = await redis.set(key, "1", ex=30, nx=True)  # 30 second TTL
            return bool(acquired)
    except Exception as e:
        logger.warning(f"获取 Redis 平仓锁失败 pos={position_id}: {e}")
    # Fallback: 内存锁(Redis 不可用时)
    if position_id in _closing_positions:
        return False
    _closing_positions.add(position_id)
    return True


async def _remove_closing_position(position_id: int) -> None:
    """释放平仓锁。"""
    try:
        redis = await get_redis()
        if redis:
            await redis.delete(f"closing_pos:{position_id}")
            return
    except Exception as e:
        logger.warning(f"释放 Redis 平仓锁失败 pos={position_id}: {e}")
    # Fallback: 内存锁
    _closing_positions.discard(position_id)


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
    # 计算含手续费的净未实现盈亏
    # 已付开仓手续费分摊(按剩余数量占初始数量比例)
    remaining_entry_fee = 0.0
    if position.initial_qty and position.initial_qty > 0:
        remaining_entry_fee = (position.entry_fee or 0) * (position.qty / position.initial_qty)
    # 估算平仓手续费(按默认 TAKER 费率计算,实际以平仓时交易所返回为准)
    est_close_fee = current_price * position.qty * CLOSE_FEE_RATE if current_price > 0 else 0.0
    net_pnl = pnl - remaining_entry_fee - est_close_fee
    cost = position.entry_price * position.qty
    net_pct = (net_pnl / cost * 100) if cost > 0 else 0.0
    return {
        "id": position.id,
        "customer_id": position.customer_id,
        "kol_id": position.kol_id,
        "kol_name": kol_name,
        "parent_id": position.parent_id,
        "batch_no": position.batch_no,
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
        "entry_fee": position.entry_fee or 0.0,
        "current_price": current_price,
        "unrealized_pnl": pnl,
        "pnl_pct": pct,
        "net_unrealized_pnl": net_pnl,
        "net_pnl_pct": net_pct,
        "est_close_fee": est_close_fee,
        "opened_at": position.opened_at.isoformat() if position.opened_at else None,
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
    }


async def _get_cached_price(exchange: str, symbol: str) -> float | None:
    """优先从 Redis 读取价格缓存,未命中则返回 None。"""
    try:
        redis = await get_redis()
        if redis is None:
            return None
        key = f"dcq:price:{exchange}:{symbol}"
        cached = await redis.get(key)
        if cached:
            price = float(cached)
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"读取价格缓存失败 {exchange}:{symbol}: {e}")
    return None


async def _set_cached_price(exchange: str, symbol: str, price: float) -> None:
    """将价格写入 Redis 缓存,容忍失败不打断主流程。"""
    try:
        redis = await get_redis()
        if redis is None:
            return
        key = f"dcq:price:{exchange}:{symbol}"
        await redis.setex(key, PRICE_CACHE_TTL_SECONDS, str(price))
    except Exception as e:
        logger.warning(f"写入价格缓存失败 {exchange}:{symbol}: {e}")


async def _check_one_position(db: AsyncSession, position: Position) -> None:
    """检查单个持仓的止盈止损/成本保护/追踪止损触发(优先读缓存)。"""
    if position.status != "open" or position.qty <= 0:
        return
    current_price = await _get_cached_price(position.exchange, position.symbol)
    if not current_price or current_price <= 0:
        current_price = await exchange_adapter.fetch_market_price(position.exchange, position.symbol)
        if current_price and current_price > 0:
            await _set_cached_price(position.exchange, position.symbol, current_price)
    if not current_price or current_price <= 0:
        return
    await _check_one_position_with_price(db, position, current_price)


async def _check_one_position_with_price(
    db: AsyncSession,
    position: Position,
    current_price: float,
    full_check: bool = True,
) -> None:
    """检查单个持仓触发条件。

    full_check=False 时只检查止损,用于统一 1 秒循环的快速路径。
    full_check=True 时检查止盈/成本保护/追踪止损,默认保持兼容。
    """
    if position.status != "open" or position.qty <= 0:
        return
    if not current_price or current_price <= 0:
        return

    # 止损触发 → 全部平仓
    if should_trigger_sl(position, current_price):
        logger.info(f"止损触发 pos={position.id} {position.symbol} price={current_price} sl={position.sl}")
        # 使用 Redis 锁防止重复平仓(与 stop_loss_monitor_loop 一致)
        if not await _add_closing_position(position.id):
            return  # 已有平仓进行中
        try:
            await order_manager.close_position(db, position.id, position.qty)
        finally:
            await _remove_closing_position(position.id)
        return

    if not full_check:
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
    # 再次校验仓位仍为 open(防止并发平仓后对已关闭仓位写入止损)
    if position.status != "open" or position.qty <= 0:
        return
    callback = position.trailing_callback
    if position.side == "long":
        new_sl = current_price * (1 - callback)
        if not position.sl or new_sl > position.sl:
            position.sl = new_sl
            try:
                await db.flush()
                await db.commit()
            except Exception as e:
                logger.warning(f"追踪止损提交失败 pos={position.id}: {e}")
                await db.rollback()
    else:
        new_sl = current_price * (1 + callback)
        if not position.sl or new_sl < position.sl:
            position.sl = new_sl
            try:
                await db.flush()
                await db.commit()
            except Exception as e:
                logger.warning(f"追踪止损提交失败 pos={position.id}: {e}")
                await db.rollback()


async def check_and_close_timeout_positions(db: AsyncSession) -> int:
    """检查超时持仓并自动平仓。

    每个客户使用自己的配置(RiskConfig.position_timeout_hours):
      - 0 表示禁用超时平仓
      - >0 表示持仓超过 N 小时后自动平仓
    未配置的客户使用默认值 48 小时。

    场景: KOL 发出开仓信号但长期未补止盈止损,持仓超时后自动平仓保护资金。

    Returns: 自动平仓的持仓数量
    """
    now = datetime.now(timezone.utc)

    # 获取所有有持仓的客户及其风控配置
    customer_ids = (
        await db.execute(
            select(Position.customer_id)
            .where(Position.status == "open")
            .distinct()
        )
    ).scalars().all()

    if not customer_ids:
        return 0

    closed_count = 0
    for cid in customer_ids:
        # 获取该客户的风控配置
        cfg_stmt = select(RiskConfig).where(
            RiskConfig.customer_id == cid,
            RiskConfig.enabled.is_(True),
        )
        cfg = (await db.execute(cfg_stmt)).scalars().first()
        timeout_hours = cfg.position_timeout_hours if cfg else DEFAULT_POSITION_TIMEOUT_HOURS

        # 0 = 禁用超时平仓
        if timeout_hours <= 0:
            continue

        cutoff = now - timedelta(hours=timeout_hours)

        # 只查子仓位(parent_id IS NOT NULL),与 monitor_loop 一致。
        # 若查到 master 并 close_position(master),会同时关闭其所有子仓位,
        # 而子仓位随后也会进入超时列表被再次 close → 重复平仓/报错。
        positions = (
            await db.execute(
                select(Position).where(
                    Position.customer_id == cid,
                    Position.status == "open",
                    Position.parent_id.is_not(None),
                    Position.opened_at < cutoff,
                )
            )
        ).scalars().all()

        price_cache: dict[tuple[str, str], float] = {}
        symbols_by_exchange: dict[str, set[str]] = {}
        for pos in positions:
            symbols_by_exchange.setdefault(pos.exchange, set()).add(pos.symbol)
        for exh, syms in symbols_by_exchange.items():
            try:
                prices = await price_feed.fetch_market_prices(exh, list(syms))
                for sym, price in prices.items():
                    if price and price > 0:
                        price_cache[(exh, sym)] = price
            except Exception as e:
                logger.warning(f"超时检查批量价格获取失败 {exh}:{sorted(syms)}: {e}")

        for pos in positions:
            try:
                current_price = price_cache.get((pos.exchange, pos.symbol), 0)
                pnl, pct = compute_pnl(pos, current_price) if current_price and current_price > 0 else (0.0, 0.0)
                if pos.sl and pos.sl > 0 and current_price and current_price > 0 and pnl > 0:
                    logger.warning(
                        f"持仓超时但已有止损且当前盈利,仅告警不强平 pos={pos.id} symbol={pos.symbol} "
                        f"opened={pos.opened_at} timeout={timeout_hours}h price={current_price} pnl={pnl:.2f}"
                    )
                    try:
                        from app.services.notification import notify
                        from app.services.order_manager import _get_position_source_text, _get_kol_name
                        _timeout_src = await _get_position_source_text(db, pos.id, pos.kol_id, pos.symbol)
                        _timeout_kol = await _get_kol_name(db, pos.kol_id)
                        await notify(
                            "risk", "持仓超时但保留盈利单",
                            f"品种: {pos.symbol}\n方向: {pos.side}\n"
                            f"持仓时间: 超过 {timeout_hours} 小时\n"
                            f"当前价: {current_price}\n浮盈: {pnl:.2f} USDT({pct:.2f}%)\n"
                            f"已有止损: {pos.sl}\n处理: 仅告警,不强制平仓",
                            pos.customer_id,
                            source_text=_timeout_src,
                            kol_name=_timeout_kol,
                        )
                    except Exception as e:
                        logger.warning(f"发送超时盈利保留通知失败 pos={pos.id}: {e}")
                    continue

                logger.warning(
                    f"持仓超时自动平仓 pos={pos.id} symbol={pos.symbol} "
                    f"opened={pos.opened_at} timeout={timeout_hours}h"
                )
                result = await order_manager.close_position(db, pos.id, pos.qty)
                if result.get("ok"):
                    closed_count += 1
                    from app.services.notification import notify
                    from app.services.order_manager import _get_position_source_text, _get_kol_name
                    _timeout_src = await _get_position_source_text(db, pos.id, pos.kol_id, pos.symbol)
                    _timeout_kol = await _get_kol_name(db, pos.kol_id)
                    await notify(
                        "tp_sl", "持仓超时自动平仓",
                        f"品种: {pos.symbol}\n方向: {pos.side}\n"
                        f"持仓时间: 超过 {timeout_hours} 小时\n"
                        f"盈亏: {result.get('pnl', 0):.2f} USDT(净,已扣手续费)",
                        pos.customer_id,
                        source_text=_timeout_src,
                        kol_name=_timeout_kol,
                    )
                else:
                    logger.warning(f"超时平仓未成功 pos={pos.id}: {result.get('reason')}")
            except Exception as e:
                # P1-13: 超时强制平仓失败时,记录详细错误原因
                logger.exception(
                    f"超时平仓失败 pos={pos.id} symbol={pos.symbol} side={pos.side} "
                    f"opened={pos.opened_at} timeout={timeout_hours}h customer={pos.customer_id}: {e}"
                )
                # 回滚会话,避免单笔失败污染后续平仓
                await db.rollback()
                # P1-13: 发送平仓失败通知,确保错误原因被记录
                try:
                    from app.services.notification import notify
                    await notify(
                        "error", "超时平仓失败",
                        f"品种: {pos.symbol}\n方向: {pos.side}\n仓位ID: {pos.id}\n"
                        f"超时时间: {timeout_hours}小时\n失败原因: {e}",
                        pos.customer_id,
                    )
                except Exception as notify_err:
                    logger.warning(f"发送超时平仓失败通知失败 pos={pos.id}: {notify_err}")

    if closed_count > 0:
        logger.info(f"超时平仓完成: {closed_count} 个持仓已自动关闭")
    return closed_count


async def monitor_loop() -> None:
    """统一持仓监控循环:1 秒检查 SL,每 5 秒检查 TP/成本保护/追踪止损。

    只检查子仓位(parent_id IS NOT NULL),不检查 master 仓位。
    统一循环避免原 1 秒 SL 循环和 5 秒 TP 循环重复查询 DB、重复拉行情。
    """
    logger.info("统一持仓监控循环已启动 (1秒SL / 5秒TP+成本保护+追踪止损)")
    tick = 0
    while True:
        try:
            tick += 1
            full_check = (tick % 5 == 0)
            async with AsyncSessionLocal() as db:
                await db.execute(text("SET LOCAL statement_timeout = '5s'"))
                result = await db.execute(
                    select(Position).where(
                        Position.status == "open",
                        Position.parent_id.is_not(None),
                    )
                )
                positions = result.scalars().all()
                if not positions:
                    await asyncio.sleep(1)
                    continue

                exchange_symbols: dict[str, set[str]] = {}
                for pos in positions:
                    exchange_symbols.setdefault(pos.exchange, set()).add(pos.symbol)

                price_cache: dict[tuple[str, str], float] = {}
                for exh, syms in exchange_symbols.items():
                    try:
                        prices = await price_feed.fetch_market_prices(exh, list(syms))
                        for sym, price in prices.items():
                            if price and price > 0:
                                price_cache[(exh, sym)] = price
                                _task = asyncio.create_task(_set_cached_price(exh, sym, price))
                    except Exception as e:
                        logger.debug(f"统一监控批量获取价格失败 {exh}:{sorted(syms)}: {e}")

                for pos in positions:
                    try:
                        current_price = price_cache.get((pos.exchange, pos.symbol))
                        if not current_price or current_price <= 0:
                            continue
                        await _check_one_position_with_price(db, pos, current_price, full_check=full_check)
                    except Exception as e:
                        logger.exception(f"检查持仓 {pos.id} 失败: {e}")
                        await db.rollback()

                if full_check:
                    _refresh_cids = {p.customer_id for p in positions if p.status == "open"}
                    for _cid in _refresh_cids:
                        try:
                            await bus.publish_customer(_cid, "position", {"action": "refresh"})
                        except Exception as e:
                            logger.debug(f"推送客户持仓刷新失败 customer={_cid}: {e}")
        except Exception as e:
            logger.exception(f"统一持仓监控循环异常: {e}")
        await asyncio.sleep(1)


async def stop_loss_monitor_loop() -> None:
    """1秒级止损监控循环:仅检查止损触发,确保快速响应。

    与5秒级的 monitor_loop 并行运行,但只关注止损:
    - 查询所有有止损的 open 子仓位
    - 每秒检查价格是否触及止损线
    - 触发后立即市价平仓

    设计借鉴KOL跟单系统:使用内部1秒轮询而非交易所算法止损单,
    确保只平该KOL的持仓,不影响其他KOL同币种仓位。

    注:此循环每秒查询所有有止损的 open 子仓位。
    确保 Position 表在 (status, parent_id, sl) 上有合适索引以提高查询效率。
    已添加 statement_timeout 防止单次查询阻塞止损监控。
    """
    logger.info("止损监控循环(1秒级)已启动")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # P3-5 修复: 设置查询超时,防止长查询阻塞止损监控
                await db.execute(text("SET LOCAL statement_timeout = '5s'"))

                # 只查有止损的 open 子仓位
                positions = (
                    await db.execute(
                        select(Position).where(
                            Position.status == "open",
                            Position.parent_id.is_not(None),
                            Position.sl.is_not(None),
                            Position.sl > 0,
                        )
                    )
                ).scalars().all()

                if not positions:
                    await asyncio.sleep(1)
                    continue

                # 按 exchange+symbol 分组,优先读缓存,未命中时批量获取价格。
                price_cache: dict[tuple[str, str], float] = {}
                missing_symbols: dict[str, set[str]] = {}
                for pos in positions:
                    key = (pos.exchange, pos.symbol)
                    if key in price_cache:
                        continue
                    cached = await _get_cached_price(pos.exchange, pos.symbol)
                    if cached and cached > 0:
                        price_cache[key] = cached
                    else:
                        missing_symbols.setdefault(pos.exchange, set()).add(pos.symbol)

                for exh, syms in missing_symbols.items():
                    try:
                        prices = await price_feed.fetch_market_prices(exh, list(syms))
                        for sym, price in prices.items():
                            if price and price > 0:
                                price_cache[(exh, sym)] = price
                                await _set_cached_price(exh, sym, price)
                    except Exception as e:
                        logger.debug(f"批量获取止损价格失败 {exh}:{sorted(syms)}: {e}")

                # 检查每个持仓的止损
                for pos in positions:
                    try:
                        key = (pos.exchange, pos.symbol)
                        current_price = price_cache.get(key, 0)
                        if not current_price or current_price <= 0:
                            continue

                        # 检查止损触发
                        if should_trigger_sl(pos, current_price):
                            # Redis锁:跳过正在平仓中的仓位,防止重复触发(跨进程)
                            if not await _add_closing_position(pos.id):
                                continue
                            logger.info(
                                f"[1s止损] 触发 pos={pos.id} {pos.symbol} "
                                f"price={current_price} sl={pos.sl}"
                            )
                            try:
                                await order_manager.close_position(db, pos.id, pos.qty)
                                await db.commit()
                            except Exception as close_err:
                                err_msg = str(close_err)
                                # 交易所返回"无持仓"时,说明仓位已在交易所端平掉,
                                # 本地状态未同步 → 强制标记为closed防止无限循环
                                if "don't have any positions" in err_msg or "no position" in err_msg.lower():
                                    logger.warning(
                                        f"[1s止损] 仓位 {pos.id} 交易所无持仓,强制关闭本地记录"
                                    )
                                    try:
                                        await db.rollback()
                                        # 用新session直接更新状态
                                        from sqlalchemy import update as sa_update
                                        await db.execute(
                                            sa_update(Position)
                                            .where(Position.id == pos.id)
                                            .values(status="closed", qty=0, closed_at=datetime.now(timezone.utc))
                                        )
                                        await db.commit()
                                    except Exception as e:
                                        logger.exception(f"[1s止损] 强制关闭本地仓位失败 pos={pos.id}: {e}")
                                        await db.rollback()
                                else:
                                    raise
                            finally:
                                await _remove_closing_position(pos.id)
                    except Exception as e:
                        logger.exception(f"[1s止损] 平仓失败 pos={pos.id}: {e}")
                        await db.rollback()

        except Exception as e:
            logger.exception(f"止损监控循环异常: {e}")

        await asyncio.sleep(1)
