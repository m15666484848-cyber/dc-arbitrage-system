"""持仓管理:实时盈亏、止盈止损触发、成本保护触发、追踪止损、监控循环。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.core.config import settings as _cfg
from app.models.config import RiskConfig
from app.models.trading import Position
from app.services import exchange_adapter, order_manager, price_feed
from app.services.circuit_breaker import get_breaker
from app.services.event_bus import bus

# 内存锁:记录正在平仓中的仓位ID,防止止损监控循环重复触发同一仓位
# 作为 Redis 不可用时的进程内兜底
_closing_positions: set[int] = set()

# 保存 asyncio.create_task 的引用,防止任务被 GC 回收
_pending_tasks: set = set()

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
            acquired = await redis.set(key, "1", ex=60, nx=True)  # 60 second TTL
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
    # S18: 强制 float 转换,防止 DB 返回 Decimal 导致类型混用
    entry_price = float(position.entry_price or 0)
    qty = float(position.qty or 0)
    current_price = float(current_price)
    if position.side == "long":
        pnl = (current_price - entry_price) * qty
    else:
        pnl = (entry_price - current_price) * qty
    cost = entry_price * qty
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


async def _get_timeout_phase(position, redis_conn=None) -> int:
    """获取持仓当前的超时保护阶段 (0=无, 1=4h, 2=24h, 3=72h, 4=96h)."""
    try:
        if redis_conn is None:
            from app.core.redis import get_redis
            redis_conn = await get_redis()
        if redis_conn:
            phase_key = f"dcq:tpsl_timeout:{position.id}"
            cached = await redis_conn.get(phase_key)
            if cached:
                return int(cached)
    except Exception:
        pass
    return 0


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
        "exchange_account_id": position.exchange_account_id,
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
        "tp_sl_source": getattr(position, "tp_sl_source", "kol") or "kol",
        "timeout_phase": await _get_timeout_phase(position, redis_conn=None),
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


async def _check_one_position_with_price(db: AsyncSession, position: Position, current_price: float, full_check: bool = True) -> None:
    """检查单个持仓的止盈止损/成本保护/追踪止损触发(使用已获取的价格)。"""
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
        if not await _add_closing_position(position.id):
            logger.info(f"止盈平仓跳过:已有平仓进行中 pos={position.id}")
            return
        try:
            await order_manager.close_at_tp_level(db, position, tp_level, current_price)
        finally:
            await _remove_closing_position(position.id)
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
    # S2修复: 使用行锁防止与close_position竞态(TOCTOU)
    # 重新查询并锁定行,确保检查和更新之间不会被并发平仓打断
    locked = (await db.execute(
        select(Position).where(Position.id == position.id).with_for_update()
    )).scalar_one_or_none()
    if not locked or locked.status != "open" or locked.qty <= 0:
        return
    callback = locked.trailing_callback
    if locked.side == "long":
        new_sl = current_price * (1 - callback)
        if not locked.sl or new_sl > locked.sl:
            locked.sl = new_sl
            try:
                if hasattr(db, "flush"):
                    await db.flush()
                await db.commit()
            except Exception as e:
                logger.warning(f"追踪止损提交失败 pos={locked.id}: {e}")
                await db.rollback()
    else:
        new_sl = current_price * (1 + callback)
        if not locked.sl or new_sl < locked.sl:
            locked.sl = new_sl
            try:
                if hasattr(db, "flush"):
                    await db.flush()
                await db.commit()
            except Exception as e:
                logger.warning(f"追踪止损提交失败 pos={locked.id}: {e}")
                await db.rollback()


async def check_orphaned_master_positions(db: AsyncSession) -> int:
    """检查孤立主仓位并自动平仓。

    扫描所有 open 主仓位(parent_id IS NULL)，如果：
    1. 没有任何 open 子仓位
    2. 超过超时时间

    则自动平仓。处理 KOL 信号创建了主仓位但子仓位从未创建、
    或所有子仓位已关闭但主仓位遗留的情况。

    Returns: 自动平仓的持仓数量
    """
    now = datetime.now(timezone.utc)

    # 1. 收集所有需要处理的孤立主仓位
    #    提前提取所有属性，避免后续 MissingGreenlet 错误
    masters_raw = (
        await db.execute(
            select(Position).where(
                Position.status == "open",
                Position.parent_id.is_(None),
            )
        )
    ).scalars().all()

    if not masters_raw:
        return 0

    # 2. 筛选真正需要处理的孤立主仓位
    candidates = []
    for master in masters_raw:
        # 检查是否有 open 子仓位
        open_children = (
            await db.execute(
                select(Position.id).where(
                    Position.parent_id == master.id,
                    Position.status == "open",
                )
            )
        ).scalars().all()

        if open_children:
            continue  # 有 open 子仓位，跳过

        # 获取超时配置
        cfg_stmt = select(RiskConfig).where(
            RiskConfig.customer_id == master.customer_id,
            RiskConfig.enabled.is_(True),
        )
        cfg = (await db.execute(cfg_stmt)).scalars().first()
        timeout_hours = cfg.position_timeout_hours if cfg else DEFAULT_POSITION_TIMEOUT_HOURS

        if timeout_hours <= 0:
            continue

        if not master.opened_at:
            continue

        opened_at = master.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)

        age_hours = (now - opened_at).total_seconds() / 3600
        if age_hours < timeout_hours:
            continue

        # 提前提取所有 ORM 属性到普通变量，避免后续 MissingGreenlet
        candidates.append({
            "id": master.id,
            "symbol": master.symbol,
            "side": master.side,
            "qty": float(master.qty) if master.qty else 0.0,
            "customer_id": master.customer_id,
            "kol_id": master.kol_id,
            "exchange": master.exchange,
            "opened_at": master.opened_at,
            "timeout_hours": timeout_hours,
            "age_hours": age_hours,
        })

    if not candidates:
        return 0

    logger.info(f"发现 {len(candidates)} 个孤立主仓位待处理")

    # 3. 使用独立 session 逐个平仓（隔离异常影响）
    closed_count = 0
    for m in candidates:
        if not await _add_closing_position(m["id"]):
            logger.info(f"孤立主仓位平仓跳过:已有平仓进行中 pos={m['id']}")
            continue

        pos_id = m["id"]
        symbol = m["symbol"]
        side = m["side"]
        qty = m["qty"]
        timeout_h = m["timeout_hours"]
        age_h = m["age_hours"]

        try:
            logger.warning(
                f"孤立主仓位超时平仓 pos={pos_id} symbol={symbol} "
                f"opened={m['opened_at']} timeout={timeout_h}h age={age_h:.1f}h "
                f"(无open子仓位)"
            )

            # 使用新 session 进行平仓，隔离异常
            async with AsyncSessionLocal() as close_db:
                try:
                    result = await order_manager.close_position(close_db, pos_id, qty)

                    if result.get("ok"):
                        closed_count += 1
                        logger.info(
                            f"孤立主仓位平仓成功 pos={pos_id} symbol={symbol} "
                            f"pnl={result.get('pnl', 0)}"
                        )
                        # 发送通知
                        try:
                            from app.services.notification import notify
                            from app.services.order_manager import _get_position_source_text, _get_kol_name
                            _src = await _get_position_source_text(close_db, pos_id, m["kol_id"], symbol)
                            _kol = await _get_kol_name(close_db, m["kol_id"])
                            await notify(
                                "tp_sl", "孤立主仓位超时自动平仓",
                                f"品种: {symbol}\n方向: {side}\n"
                                f"持仓时间: 超过 {timeout_h} 小时 (无子仓位)\n"
                                f"盈亏: {float(result.get('pnl', 0)):.2f} USDT",
                                m["customer_id"],
                                source_text=_src,
                                kol_name=_kol,
                            )
                        except Exception:
                            pass
                    else:
                        reason = str(result.get("reason", ""))
                        logger.warning(f"孤立主仓位平仓未成功 pos={pos_id}: {reason}")

                        # 交易所无持仓 -> 强制关闭本地记录
                        reason_lower = reason.lower()
                        if any(kw in reason_lower for kw in [
                            "no position", "don't have", "not found",
                            "does not exist", "no open", "position size is zero",
                            "insufficient", "order does not exist"
                        ]):
                            from sqlalchemy import update as sa_update, text as sa_text
                            await close_db.execute(
                                sa_update(Position)
                                .where(Position.id == pos_id)
                                .values(
                                    status="closed",
                                    qty=0,
                                    closed_at=datetime.now(timezone.utc),
                                )
                            )
                            await close_db.commit()
                            closed_count += 1
                            logger.info(
                                f"孤立主仓位强制关闭(交易所无持仓) "
                                f"pos={pos_id} symbol={symbol}"
                            )
                        else:
                            await close_db.rollback()

                except Exception as inner_e:
                    err_msg = str(inner_e).lower()
                    logger.exception(
                        f"孤立主仓位平仓异常 pos={pos_id} symbol={symbol}: {inner_e}"
                    )
                    await close_db.rollback()

                    # 交易所无持仓 -> 强制关闭
                    if any(kw in err_msg for kw in [
                        "no position", "don't have", "not found",
                        "does not exist", "no open", "position size is zero",
                        "insufficient", "order does not exist",
                        "bad symbol", "invalid symbol", "market"
                    ]):
                        try:
                            async with AsyncSessionLocal() as fix_db:
                                from sqlalchemy import update as sa_update
                                await fix_db.execute(
                                    sa_update(Position)
                                    .where(Position.id == pos_id)
                                    .values(
                                        status="closed",
                                        qty=0,
                                        closed_at=datetime.now(timezone.utc),
                                    )
                                )
                                await fix_db.commit()
                                closed_count += 1
                                logger.info(
                                    f"孤立主仓位强制关闭(异常-交易所无持仓) "
                                    f"pos={pos_id} symbol={symbol}"
                                )
                        except Exception as fix_err:
                            logger.error(
                                f"孤立主仓位强制关闭失败 pos={pos_id}: {fix_err}"
                            )

        except Exception as outer_e:
            logger.exception(
                f"孤立主仓位处理失败 pos={pos_id} symbol={symbol}: {outer_e}"
            )
        finally:
            await _remove_closing_position(pos_id)

    if closed_count > 0:
        logger.info(f"孤立主仓位超时平仓完成: {closed_count}/{len(candidates)} 个持仓已关闭")
    else:
        logger.info(f"孤立主仓位扫描完成: 0/{len(candidates)} 个持仓被关闭")
    return closed_count


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

        for pos in positions:
            if not await _add_closing_position(pos.id):
                logger.info(f"超时平仓跳过:已有平仓进行中 pos={pos.id}")
                continue
            try:
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
                except Exception:
                    pass
            finally:
                await _remove_closing_position(pos.id)

    if closed_count > 0:
        logger.info(f"超时平仓完成: {closed_count} 个持仓已自动关闭")



    # 检查孤立主仓位
    try:
        orphan_closed = await check_orphaned_master_positions(db)
        closed_count += orphan_closed
    except Exception as e:
        logger.exception(f"孤立主仓位检查失败: {e}")

    return closed_count



# ---------------------------------------------------------------------------
# 超时分级保护: KOL 未提供止盈止损时的自动风控
# ---------------------------------------------------------------------------

# 超时阈值 (小时)
TPSL_TIMEOUT_PHASE1_HOURS = 4       # 启用追踪止损
TPSL_TIMEOUT_PHASE2_HOURS = 24      # 收紧追踪止损回撤
TPSL_TIMEOUT_PHASE3_HOURS = 72      # 告警用户手动决策
TPSL_TIMEOUT_PHASE4_HOURS = 96      # 自动市价平仓 (72h告警 + 24h宽限期)

# 追踪止损参数
TIMEOUT_TRAILING_CALLBACK_PHASE1 = 0.03   # 3% 回撤
TIMEOUT_TRAILING_CALLBACK_PHASE2 = 0.02   # 2% 回撤 (收紧)

# 默认超时配置 (可被策略 params 覆盖)
DEFAULT_TIMEOUT_CONFIG = {
    "enabled": True,          # 超时保护总开关
    "phase1_hours": 4,        # 启用追踪止损
    "phase2_hours": 24,       # 收紧追踪回撤
    "phase3_hours": 72,       # 告警用户
    "phase4_hours": 96,       # 自动平仓
    "trailing_p1": 0.03,      # Phase1 追踪回撤
    "trailing_p2": 0.02,      # Phase2 追踪回撤
}


async def _get_strategy_timeout_config(db, pos) -> dict:
    """从持仓关联的策略获取超时保护配置, 无策略则用默认值."""
    cfg = dict(DEFAULT_TIMEOUT_CONFIG)
    try:
        from app.models.strategy import Strategy
        from sqlalchemy import select as _sel
        # 通过 exchange_account_id 找 strategy_id
        from app.models.config import ExchangeAccount
        ea_result = await db.execute(
            _sel(ExchangeAccount).where(ExchangeAccount.id == pos.exchange_account_id)
        )
        ea = ea_result.scalar_one_or_none()
        if ea and ea.strategy_id:
            s_result = await db.execute(
                _sel(Strategy).where(Strategy.id == ea.strategy_id)
            )
            strategy = s_result.scalar_one_or_none()
            if strategy and strategy.params:
                p = strategy.params
                if "timeout_protection_enabled" in p:
                    cfg["enabled"] = p["timeout_protection_enabled"]
                if "timeout_phase1_hours" in p:
                    cfg["phase1_hours"] = p["timeout_phase1_hours"]
                if "timeout_phase2_hours" in p:
                    cfg["phase2_hours"] = p["timeout_phase2_hours"]
                if "timeout_phase3_hours" in p:
                    cfg["phase3_hours"] = p["timeout_phase3_hours"]
                if "timeout_phase4_hours" in p:
                    cfg["phase4_hours"] = p["timeout_phase4_hours"]
                if "timeout_trailing_p1" in p:
                    cfg["trailing_p1"] = p["timeout_trailing_p1"]
                if "timeout_trailing_p2" in p:
                    cfg["trailing_p2"] = p["timeout_trailing_p2"]
    except Exception:
        pass
    return cfg


async def check_and_apply_tpsl_timeout_protection(db: AsyncSession) -> int:
    """超时分级保护: 对持仓时间过长且无 KOL 止盈止损管理的仓位自动加保护。

    分级策略:
      Phase 1 (4h):  启用追踪止损 (3% 回撤), 防止盈利回吐
      Phase 2 (24h): 收紧追踪止损回撤至 2%
      Phase 3 (72h): 飞书告警, 提示用户手动决策

    使用 Redis key dcq:tpsl_timeout:{position_id} 记录已执行的阶段, 避免重复处理。
    每个阶段只执行一次。

    Returns: 处理的持仓数量
    """
    now = datetime.now(timezone.utc)
    try:
        redis = await get_redis()
    except Exception as e:
        logger.warning(f"check_and_apply_tpsl_timeout_protection get_redis 失败, redis=None: {e}")
        redis = None

    # 查询所有 open 子仓位 (仅限无 KOL 止盈止损管理的持仓)
    # tp_sl_source = 'kol' 表示 KOL 明确提供了止盈止损, 不需要超时保护
    # tp_sl_source = 'default' 或 'timeout' 或 NULL 表示需要系统保护
    result = await db.execute(
        select(Position).where(
            Position.status == "open",
            Position.parent_id.is_not(None),
            or_(Position.tp_sl_source.is_(None), Position.tp_sl_source != "kol"),
        )
    )
    positions = result.scalars().all()
    if not positions:
        return 0

    processed = 0
    from app.services.notification import notify

    for pos in positions:
        if not pos.opened_at:
            continue

        # 获取此持仓关联的策略超时配置
        timeout_cfg = await _get_strategy_timeout_config(db, pos)
        if not timeout_cfg["enabled"]:
            continue

        # 如果持仓已有 KOL 止盈止损 (tp_sl_source='kol'), 跳过
        if getattr(pos, 'tp_sl_source', '') == 'kol':
            continue

        # 如果策略已配置追踪止损, Phase1 不应覆盖
        strategy_has_trailing = pos.trailing_stop and pos.trailing_callback > 0

        # 计算持仓时长 (小时)
        opened_at = pos.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        age_hours = (now - opened_at).total_seconds() / 3600

        # Redis key 记录已执行的阶段
        phase_key = f"dcq:tpsl_timeout:{pos.id}"
        try:
            completed_phase = 0
            if redis:
                cached = await redis.get(phase_key)
                if cached:
                    completed_phase = int(cached)
        except Exception:
            completed_phase = 0

        # Phase 4: 96h 自动市价平仓 (72h告警 + 24h宽限期)
        if age_hours >= timeout_cfg["phase4_hours"] and completed_phase < 4:
            phase4_ok = False
            try:
                # 使用 Redis 锁防止与其他平仓逻辑并发
                if not await _add_closing_position(pos.id):
                    logger.debug(f"Phase4 跳过(正在平仓中) pos={pos.id}")
                    continue
                try:
                    from app.services import order_manager as _om
                    result = await _om.close_position(db, pos.id, pos.qty)
                    if result.get("ok"):
                        phase4_ok = True
                        logger.warning(
                            f"超时分级保护 Phase4 自动平仓 pos={pos.id} {pos.symbol} "
                            f"age={age_hours:.1f}h pnl={result.get('pnl', 0):.2f}"
                        )
                        try:
                            from app.services.notification import notify
                            from app.services.order_manager import _get_position_source_text, _get_kol_name
                            _src = await _get_position_source_text(db, pos.id, pos.kol_id, pos.symbol)
                            _kol = await _get_kol_name(db, pos.kol_id)
                            await notify(
                                "tp_sl", "持仓超96小时已自动平仓",
                                "品种: " + pos.symbol + "\n"
                                "方向: " + pos.side + "\n"
                                "交易所: " + pos.exchange.upper() + "\n"
                                "持仓时间: " + f"{age_hours:.1f}" + " 小时\n"
                                "净盈亏: " + f"{result.get('pnl', 0):.2f}" + " USDT\n"
                                "说明: KOL 未提供止盈止损且超72h告警后24h未处理, 系统已自动平仓",
                                pos.customer_id,
                                source_text=_src,
                                kol_name=_kol,
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning(
                            f"Phase4 自动平仓失败 pos={pos.id}: {result.get('reason')}"
                        )
                finally:
                    await _remove_closing_position(pos.id)
            except Exception as e:
                logger.exception(f"Phase4 自动平仓异常 pos={pos.id}: {e}")
            # 仅在平仓成功时才标记 Phase4 完成, 失败时不标记以便下次循环重试
            if phase4_ok:
                try:
                    if redis:
                        await redis.setex(phase_key, 7 * 86400, "4")
                except Exception:
                    pass
            processed += 1
            continue

        # Phase 3: 72h 告警 (只执行一次)
        if age_hours >= timeout_cfg["phase3_hours"] and completed_phase < 3:
            phase3_ok = False
            try:
                from app.services.order_manager import _get_position_source_text, _get_kol_name
                _src = await _get_position_source_text(db, pos.id, pos.kol_id, pos.symbol)
                _kol = await _get_kol_name(db, pos.kol_id)
                await notify(
                    "tp_sl", "持仓超72小时需关注",
                    "品种: " + pos.symbol + "\n"
                    "方向: " + pos.side + "\n"
                    "交易所: " + pos.exchange.upper() + "\n"
                    "持仓时间: " + f"{age_hours:.1f}" + " 小时\n"
                    "入场价: " + str(pos.entry_price) + "\n"
                    "当前止损: " + str(pos.sl) + "\n"
                    "建议: 检查是否需要手动平仓或调整止盈止损",
                    pos.customer_id,
                    source_text=_src,
                    kol_name=_kol,
                )
                logger.warning(
                    f"超时分级保护 Phase3 pos={pos.id} {pos.symbol} "
                    f"age={age_hours:.1f}h"
                )
                phase3_ok = True
            except Exception as e:
                logger.warning(f"Phase3 告警失败 pos={pos.id}: {e}")
            # 仅在通知发送成功后才标记 Phase3 完成, 失败时不标记以便下次重试
            if phase3_ok:
                try:
                    if redis:
                        await redis.setex(phase_key, 7 * 86400, "3")
                except Exception:
                    pass
            processed += 1
            continue

        # Phase 2: 24h 收紧追踪止损
        if age_hours >= timeout_cfg["phase2_hours"] and completed_phase < 2:
            phase2_ok = False
            try:
                if pos.trailing_stop and pos.trailing_callback > timeout_cfg["trailing_p2"]:
                    pos.trailing_callback = timeout_cfg["trailing_p2"]
                    await db.commit()
                    logger.info(
                        f"超时分级保护 Phase2 pos={pos.id} {pos.symbol} "
                        f"trailing_callback -> {timeout_cfg['trailing_p2']}"
                    )
                    phase2_ok = True
                elif not pos.trailing_stop:
                    pos.trailing_stop = True
                    pos.trailing_callback = timeout_cfg["trailing_p2"]
                    await db.commit()
                    logger.info(
                        f"超时分级保护 Phase2 pos={pos.id} {pos.symbol} "
                        f"启用追踪止损 callback={timeout_cfg['trailing_p2']}"
                    )
                    phase2_ok = True
                else:
                    # 追踪止损已启用且回撤已 <= p2, 无需调整, 视为目标已达成
                    phase2_ok = True
            except Exception as e:
                logger.warning(f"Phase2 收紧失败 pos={pos.id}: {e}")
                await db.rollback()
            # 仅在 DB commit 成功(或无需调整)后才标记 Phase2 完成, 失败时不标记以便下次重试
            if phase2_ok:
                try:
                    if redis:
                        await redis.setex(phase_key, 7 * 86400, "2")
                except Exception:
                    pass
            processed += 1
            continue

        # Phase 1: 4h 启用追踪止损
        if age_hours >= timeout_cfg["phase1_hours"] and completed_phase < 1:
            phase1_ok = False
            try:
                # 如果策略已配置追踪止损, 不覆盖策略设置
                if strategy_has_trailing:
                    logger.debug(f"Phase1 跳过(策略已配置追踪止损) pos={pos.id}")
                    phase1_ok = True
                elif not pos.trailing_stop:
                    pos.trailing_stop = True
                    pos.trailing_callback = timeout_cfg["trailing_p1"]
                    pos.tp_sl_source = "timeout"
                    await db.commit()
                    logger.info(
                        f"超时分级保护 Phase1 pos={pos.id} {pos.symbol} "
                        f"启用追踪止损 callback={timeout_cfg['trailing_p1']}"
                    )
                    phase1_ok = True
                    try:
                        from app.services.order_manager import _get_kol_name
                        _kol = await _get_kol_name(db, pos.kol_id)
                        await notify(
                            "tp_sl", "持仓超4小时已启动追踪止损",
                            "品种: " + pos.symbol + "\n"
                            "方向: " + pos.side + "\n"
                            "交易所: " + pos.exchange.upper() + "\n"
                            "持仓时间: " + f"{age_hours:.1f}" + " 小时\n"
                            "追踪止损回撤: " + f"{timeout_cfg['trailing_p1'] * 100:.0f}" + "%\n"
                            "说明: KOL 未提供止盈止损, 系统已自动启动追踪止损保护",
                            pos.customer_id,
                            kol_name=_kol,
                        )
                    except Exception:
                        pass
                else:
                    # 已有追踪止损, 无需覆盖
                    phase1_ok = True
            except Exception as e:
                logger.warning(f"Phase1 启用追踪止损失败 pos={pos.id}: {e}")
                await db.rollback()
            # 仅在 DB commit 成功(或策略已有/无需调整)后才标记 Phase1 完成, 失败时不标记以便下次重试
            if phase1_ok:
                try:
                    if redis:
                        await redis.setex(phase_key, 7 * 86400, "1")
                except Exception:
                    pass
            processed += 1

    if processed > 0:
        logger.info(f"超时分级保护完成: 处理 {processed} 个持仓")
    return processed


async def monitor_loop() -> None:
    """后台持仓监控循环:每 5 秒检查所有 open 子仓位。

    只检查子仓位(parent_id IS NOT NULL),不检查 master 仓位。
    原因:master 和子仓位都有 tp_levels/sl 配置,如果同时检查会导致:
      1. master 触发止盈时走简单逻辑,close_position 会关闭所有子仓位(而非按比例)
      2. 子仓位触发止盈后 master 的 tp_levels 未更新,下次循环 master 会重复触发
    子仓位的 close_at_tp_level 聚合逻辑会正确同步 master 状态。

    优化:
      1. 按 exchange 分组批量查询价格,减少 API 调用次数。
      2. 批量价格写入 Redis 缓存,供 order_manager 信号处理复用。

    注:止损的快速响应已由并行的 stop_loss_monitor_loop(1秒级)承担,
    本循环仍会检查止损作为兜底,但主要职责是止盈/成本保护/追踪止损。
    """
    logger.info("持仓监控循环已启动 (5秒级: 止盈/成本保护/追踪止损)")
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
                        if price and price > 0:
                            price_cache[(exh, sym)] = price
                            # 异步写入缓存(不阻塞,失败已内部捕获)
                            _task = asyncio.create_task(_set_cached_price(exh, sym, price))
                            _pending_tasks.add(_task)
                            _task.add_done_callback(_pending_tasks.discard)

                for pos in positions:
                    pos_id = pos.id
                    pos_exchange = pos.exchange
                    pos_symbol = pos.symbol
                    pos_customer_id = pos.customer_id
                    try:
                        current_price = price_cache.get((pos_exchange, pos_symbol))
                        if not current_price or current_price <= 0:
                            continue
                        await _check_one_position_with_price(db, pos, current_price)
                    except Exception as e:
                        logger.exception(f"检查持仓 {pos_id} 失败: {e}")
                        # 回滚会话,防止单笔异常(close_position 中途失败)污染后续持仓检查
                        await db.rollback()
                # 推送持仓更新事件,触发前端实时刷新(价格/盈亏/止损/成本保护/追踪止损)
                _refresh_cids = set()
                for _pos in positions:
                    try:
                        if _pos.status == "open":
                            _refresh_cids.add(_pos.customer_id)
                    except Exception:
                        # ORM 对象可能在回滚后失效,跳过
                        pass
                for _cid in _refresh_cids:
                    try:
                        await bus.publish_customer(_cid, "position", {"action": "refresh"})
                    except Exception:
                        pass
        except Exception as e:
            logger.exception(f"持仓监控循环异常: {e}")
        await asyncio.sleep(5)


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

                # 按 exchange 分组,批量获取价格；缓存命中优先，未命中再批量请求。
                price_cache: dict[tuple[str, str], float] = {}
                missing_by_exchange: dict[str, set[str]] = {}
                for pos in positions:
                    key = (pos.exchange, pos.symbol)
                    if key in price_cache:
                        continue
                    cached = await _get_cached_price(pos.exchange, pos.symbol)
                    if cached and cached > 0:
                        price_cache[key] = cached
                    else:
                        missing_by_exchange.setdefault(pos.exchange, set()).add(pos.symbol)

                for exchange, symbols in missing_by_exchange.items():
                    # P0修复: 使用断路器保护,防止交易所API超时拖垮1秒止损循环
                    cb = get_breaker(f"price_fetch:{exchange}", threshold=3, recovery_time=60)
                    if not cb.can_call():
                        logger.debug(f"[断路器] {exchange} API暂停中,使用缓存价格")
                        prices = {}
                    else:
                        try:
                            prices = await exchange_adapter.fetch_market_prices_batch(exchange, list(symbols))
                            cb.record_success()
                        except Exception as e:
                            cb.record_failure()
                            logger.warning(f"批量获取价格失败 {exchange}:{symbols}: {e} (断路器: {cb.state.value})")
                            prices = {}
                    for symbol in symbols:
                        price = prices.get(symbol) if prices else None
                        if price and price > 0:
                            price_cache[(exchange, symbol)] = price
                            await _set_cached_price(exchange, symbol, price)

                # 检查每个持仓的止损
                for pos in positions:
                    pos_id = pos.id
                    pos_exchange = pos.exchange
                    pos_symbol = pos.symbol
                    pos_side = pos.side
                    pos_qty = pos.qty
                    pos_sl = pos.sl
                    pos_customer_id = pos.customer_id
                    pos_entry_price = pos.entry_price
                    pos_realized_pnl = pos.realized_pnl
                    pos_exchange_account_id = pos.exchange_account_id
                    try:
                        key = (pos_exchange, pos_symbol)
                        current_price = price_cache.get(key, 0)
                        if not current_price or current_price <= 0:
                            continue

                        # 检查止损触发
                        if should_trigger_sl(pos, current_price):
                            # Redis锁:跳过正在平仓中的仓位,防止重复触发(跨进程)
                            if not await _add_closing_position(pos_id):
                                continue
                            logger.info(
                                f"[1s止损] 触发 pos={pos_id} {pos_symbol} "
                                f"price={current_price} sl={pos_sl}"
                            )
                            try:
                                await order_manager.close_position(db, pos_id, pos_qty)
                                # S16修复: 移除多余commit,close_position内部已管理事务
                    # await db.commit()
                            except Exception as close_err:
                                err_msg = str(close_err)
                                # 交易所返回"无持仓"时,说明仓位已在交易所端平掉,
                                # 本地状态未同步 → 强制标记为closed防止无限循环
                                if "don't have any positions" in err_msg.lower() or "no position" in err_msg.lower():
                                    logger.warning(
                                        f"[1s止损] 仓位 {pos_id} 交易所无持仓,强制关闭本地记录"
                                    )
                                    try:
                                        await db.rollback()
                                        # 尝试从交易所获取最近成交记录计算realized_pnl
                                        estimated_pnl = None
                                        try:
                                            ex_force, _ = await exchange_adapter.load_exchange(
                                                db, pos_customer_id, pos_exchange,
                                                exchange_account_id=pos_exchange_account_id,
                                            )
                                            try:
                                                # 仅取最近5分钟内的成交, 避免包含其他仓位的交易
                                                since_ms = int((datetime.now(timezone.utc).timestamp() - 300) * 1000)
                                                recent_trades = await ex_force.fetch_my_trades(
                                                    pos_symbol, since=since_ms, limit=20
                                                )
                                                close_side = "sell" if pos_side == "long" else "buy"
                                                close_trades = [t for t in recent_trades if t.get("side") == close_side]
                                                if close_trades and pos_entry_price:
                                                    total_qty = sum(float(t.get("amount", 0)) for t in close_trades)
                                                    total_value = sum(float(t.get("amount", 0)) * float(t.get("price", 0)) for t in close_trades)
                                                    if total_qty > 0:
                                                        avg_close_price = total_value / total_qty
                                                        # 用 pos_qty 限制盈亏计算的数量, 避免把其他仓位的成交算进来
                                                        calc_qty = min(total_qty, pos_qty) if pos_qty else total_qty
                                                        if pos_side == "long":
                                                            estimated_pnl = (avg_close_price - pos_entry_price) * calc_qty
                                                        else:
                                                            estimated_pnl = (pos_entry_price - avg_close_price) * calc_qty
                                                        logger.info(f"[1s止损] 仓位 {pos_id} 估算realized_pnl={estimated_pnl:.2f} (calc_qty={calc_qty})")
                                            finally:
                                                await exchange_adapter.close_exchange(ex_force)
                                        except Exception as fetch_e:
                                            logger.warning(f"[1s止损] 仓位 {pos_id} 无法从交易所获取成交记录计算realized_pnl: {fetch_e}")

                                        if estimated_pnl is None:
                                            logger.warning(f"[1s止损] 仓位 {pos_id} 无法计算realized_pnl,estimated_pnl=None,不设为0")

                                        from sqlalchemy import update as sa_update
                                        update_values: dict = {
                                            "status": "closed",
                                            "qty": 0,
                                            "closed_at": datetime.now(timezone.utc),
                                        }
                                        if estimated_pnl is not None:
                                            update_values["realized_pnl"] = (pos_realized_pnl or 0) + estimated_pnl

                                        await db.execute(
                                            sa_update(Position)
                                            .where(Position.id == pos_id)
                                            .values(**update_values)
                                        )
                                        await db.commit()
                                    except Exception:
                                        await db.rollback()
                                else:
                                    raise
                            finally:
                                await _remove_closing_position(pos_id)
                    except Exception as e:
                        logger.exception(f"[1s止损] 平仓失败 pos={pos_id}: {e}")
                        await db.rollback()

        except Exception as e:
            logger.exception(f"止损监控循环异常: {e}")

        await asyncio.sleep(1)
