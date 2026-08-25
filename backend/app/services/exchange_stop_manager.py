"""交易所侧止损单管理器。

背景: 1秒级DB止损监控在极端行情下存在明显滑点(价格急速穿越止损位时,
轮询检测+市价平仓链路慢,实测单笔滑点可达1000+点)。本模块在开仓后
同步在交易所挂 reduceOnly 条件止损单,由交易所撮合引擎在触发价直接
市价平仓,消除轮询延迟。

设计要点:
- 只为子仓位(parent_id非空)挂止损单: 主仓位是物理聚合,不单独挂
- reduceOnly 保证误触发/重复触发只会被交易所拒绝,不会反向开仓
- 15秒同步循环: 保证止损单与DB的 sl/qty 一致
  (部分止盈后数量变化 / 移动止损后价格变化 -> 自动撤旧挂新)
- 持仓全平后撤销止损单,防止孤儿止损单误伤后续新持仓
- DB的1秒止损监控保留为兜底: 交易所侧已触发时,DB平仓会收到
  "无持仓"错误,由既有S16容错路径强制关闭本地记录并估算盈亏
"""
from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.trading import Position
from app.services import exchange_adapter

SUPPORTED_EXCHANGES = {"okx", "bybit"}
SYNC_INTERVAL_SECONDS = 15
_MAX_CONSECUTIVE_FAILS = 3

_syncing: set[int] = set()
# 失败计数按 (position_id, sl, qty) 记录: 止损价或数量变化后自动重置
_fail_counts: dict[tuple, int] = {}


def _fail_key(position: Position) -> tuple:
    return (
        position.id,
        round(float(position.sl or 0), 8),
        round(float(position.qty or 0), 8),
    )


def _is_qty_consistent(position: Position) -> bool:
    return abs((position.exchange_stop_qty or 0) - (position.qty or 0)) <= max(
        (position.qty or 0) * 0.001, 1e-12
    )


def _is_price_consistent(position: Position) -> bool:
    if not position.exchange_stop_price or not position.sl:
        return False
    return abs(position.exchange_stop_price - position.sl) / position.sl <= 0.001


async def sync_exchange_stop_for_position(db: AsyncSession, position: Position) -> bool:
    """确保交易所侧存在与DB一致的止损单;已平仓/无效时撤销遗留止损单。"""
    pos_id = getattr(position, "id", None)
    if pos_id is None:
        return False
    if pos_id in _syncing:
        return False
    if (position.exchange or "").lower() not in SUPPORTED_EXCHANGES:
        return False
    # 主仓位(物理聚合)不挂交易所止损;仅子仓位独立挂单
    if position.parent_id is None:
        # ★ Fix(2026-08-25): 父仓不应持有止损单;发现遗留(历史bug产物,
        # 如#942/943镜像双止损事故)立即撤销,防止与子仓止损单并存导致
        # 双重平仓误伤同账户其他KOL仓位。
        if position.exchange_stop_order_id:
            try:
                return await _cancel_stop(db, position)
            except Exception as e:
                logger.warning(
                    f"[交易所止损] 撤销父仓遗留止损单失败 pos={pos_id}: {str(e)[:150]}"
                )
                return False
        return False

    _syncing.add(pos_id)
    try:
        active = (
            position.status == "open"
            and (position.qty or 0) > 0
            and bool(position.sl)
            and position.sl > 0
        )
        if not active:
            if position.exchange_stop_order_id:
                return await _cancel_stop(db, position)
            return False

        # ★ Fix B(2026-08-24): 止损单状态感知。有stop_id时先核实交易所侧状态:
        # 已成交 → 按成交均价记账平仓(不再发平仓单,防止吃掉同账户其他KOL仓位);
        # 已消失 → 清除字段立即重挂,内部1秒止损监控同步恢复兜底(Fix A联动)。
        if position.exchange_stop_order_id:
            _state = await _verify_stop_order_state(db, position)
            if _state == "closed":
                return True
            # gone: 字段已清空,落到下方直接挂新单; live/unknown: 继续一致性检查
        if (
            position.exchange_stop_order_id
            and _is_qty_consistent(position)
            and _is_price_consistent(position)
        ):
            return False  # 已一致且交易所侧状态正常,无需操作

        old_cancelled = False
        fk = _fail_key(position)
        if _fail_counts.get(fk, 0) >= _MAX_CONSECUTIVE_FAILS:
            return False  # 连续失败暂停,等sl/qty变化后自动重试

        try:
            ex, _ = await exchange_adapter.load_exchange(
                db,
                position.customer_id,
                position.exchange,
                exchange_account_id=position.exchange_account_id,
            )
            # 价格/数量变化时先撤旧单;撤不掉则本轮跳过,避免交易所留双份止损单
            if position.exchange_stop_order_id:
                cancelled = await exchange_adapter.cancel_native_stop_loss_order(
                    ex, position.exchange, position.symbol, position.exchange_stop_order_id
                )
                if not cancelled:
                    _fail_counts[fk] = _fail_counts.get(fk, 0) + 1
                    logger.warning(
                        f"[交易所止损] 撤旧单未确认,本轮跳过 pos={pos_id} "
                        f"stop={position.exchange_stop_order_id}"
                    )
                    return False
                old_cancelled = True
            result = await exchange_adapter.place_native_stop_loss_order(
                ex,
                position.exchange,
                position.symbol,
                position.side,
                position.qty,
                position.sl,
            )
            new_id = str(result.get("id") or "")
            if not new_id:
                raise ValueError(f"交易所未返回止损单ID: {str(result)[:200]}")
            position.exchange_stop_order_id = new_id
            position.exchange_stop_qty = position.qty
            position.exchange_stop_price = position.sl
            await db.commit()
            _fail_counts.pop(fk, None)
            logger.info(
                f"[交易所止损] 挂单成功 pos={pos_id} {position.symbol} "
                f"{position.side} qty={position.qty} 触发价={position.sl} stopId={new_id}"
            )
            return True
        except Exception as e:
            # FIX: rollback 前保存 ORM 属性,避免过期后同步懒加载触发 MissingGreenlet
            _sym = getattr(position, "symbol", "?")
            await db.rollback()
            if old_cancelled:
                # 旧止损单已撤、新单挂失败: 清除stop字段让内部1秒止损监控立即
                # 恢复兜底(Fix A有stop_id时会跳过内部监控,不清字段=止损裸奔)。
                # rollback后ORM对象已过期,只能用原生UPDATE,不能碰ORM属性。
                try:
                    from sqlalchemy import update as sa_update
                    await db.execute(
                        sa_update(Position)
                        .where(Position.id == pos_id)
                        .values(
                            exchange_stop_order_id="",
                            exchange_stop_qty=0.0,
                            exchange_stop_price=0.0,
                        )
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
            _fail_counts[fk] = _fail_counts.get(fk, 0) + 1
            logger.warning(
                f"[交易所止损] 同步失败(连续第{_fail_counts[fk]}次) pos={pos_id} "
                f"{_sym}: {str(e)[:200]}"
            )
            # ★ Fix(2026-08-24): rollback 已使对象过期,refresh 恢复,
            # 防止调用方(_place_entry/close_position)后续访问 position.* 抛 MissingGreenlet
            try:
                await db.refresh(position)
            except Exception:
                pass
            return False
    finally:
        _syncing.discard(pos_id)


async def _verify_stop_order_state(db: AsyncSession, position: Position) -> str:
    """核实交易所侧止损单状态并处理终态。

    返回:
      "closed"  止损单已成交,已调用记账平仓(仓位已closed)
      "gone"    止损单已不在交易所(被手动撤销等),字段已清除待重挂
      "live"    仍在交易所挂着,无需处理
      "unknown" 查询失败,保持现状
    """
    pos_id = position.id
    stop_id = position.exchange_stop_order_id
    try:
        ex, _ = await exchange_adapter.load_exchange(
            db, position.customer_id, position.exchange,
            exchange_account_id=position.exchange_account_id,
        )
    except Exception as e:
        logger.warning(f"[交易所止损] 状态核实加载交易所失败 pos={pos_id}: {str(e)[:200]}")
        return "unknown"
    try:
        st = await exchange_adapter.fetch_native_stop_order_status(
            ex, position.exchange, position.symbol, stop_id
        )
    except Exception as e:
        logger.debug(f"[交易所止损] 状态查询失败 pos={pos_id} stop={stop_id}: {str(e)[:200]}")
        return "unknown"

    state = str(st.get("state") or "unknown")
    if state == "filled":
        from app.services import order_manager
        result = await order_manager.close_position_by_stop_fill(
            db, pos_id, stop_id,
            fill_price=st.get("avg_price"),
            fill_qty=st.get("filled_qty"),
            close_fee=st.get("fee"),
        )
        if result.get("ok"):
            logger.info(
                f"[交易所止损] 止损单已成交,记账平仓完成 pos={pos_id} "
                f"pnl={result.get('pnl')}"
            )
            return "closed"
        logger.warning(f"[交易所止损] 止损单成交记账未成功 pos={pos_id}: {result}")
        return "unknown"
    if state == "gone":
        logger.warning(
            f"[交易所止损] 止损单已不在交易所(可能被手动撤销),清除记录待重挂 "
            f"pos={pos_id} stop={stop_id}"
        )
        try:
            position.exchange_stop_order_id = ""
            position.exchange_stop_qty = 0.0
            position.exchange_stop_price = 0.0
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning(f"[交易所止损] 清除失效止损单记录失败 pos={pos_id}: {e}")
        return "gone"
    return state


async def cancel_for_closed_position(db: AsyncSession, position: Position) -> bool:
    """持仓已全平后撤销交易所侧止损单(防止孤儿止损单误伤后续新持仓)。"""
    if position.id in _syncing:
        return False
    if not position.exchange_stop_order_id:
        return True
    _syncing.add(position.id)
    try:
        return await _cancel_stop(db, position)
    finally:
        _syncing.discard(position.id)


async def _cancel_stop(db: AsyncSession, position: Position) -> bool:
    stop_id = position.exchange_stop_order_id
    try:
        ex, _ = await exchange_adapter.load_exchange(
            db,
            position.customer_id,
            position.exchange,
            exchange_account_id=position.exchange_account_id,
        )
        cancelled = await exchange_adapter.cancel_native_stop_loss_order(
            ex, position.exchange, position.symbol, stop_id
        )
        if not cancelled:
            logger.warning(
                f"[交易所止损] 撤单未确认,保留记录待重试 pos={position.id} stop={stop_id}"
            )
            return False
        position.exchange_stop_order_id = ""
        position.exchange_stop_qty = 0.0
        position.exchange_stop_price = 0.0
        await db.commit()
        logger.info(f"[交易所止损] 已撤销 pos={position.id} stopId={stop_id}")
        return True
    except Exception as e:
        # FIX: rollback 前保存 ORM 属性,避免过期后同步懒加载触发 MissingGreenlet
        _pos_id = getattr(position, "id", "?")
        await db.rollback()
        logger.warning(
            f"[交易所止损] 撤单异常 pos={_pos_id} stop={stop_id}: {str(e)[:200]}"
        )
        return False


async def exchange_stop_sync_loop() -> None:
    """15秒级同步循环: 为open子仓位维护交易所侧止损单,清理已平仓位的遗留止损单。"""
    logger.info("交易所侧止损同步循环(15秒级)已启动")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(Position)
                    .where(
                        or_(
                            and_(
                                Position.status == "open",
                                Position.parent_id.is_not(None),
                            ),
                            and_(
                                Position.exchange_stop_order_id.is_not(None),
                                Position.exchange_stop_order_id != "",
                            ),
                        )
                    )
                    .order_by(Position.id)
                    .limit(300)
                )
                positions = (await db.execute(stmt)).scalars().all()
                # ★ Fix(2026-08-24): 循环内任一 sync 失败 rollback 会使会话中
                # 所有 ORM 对象过期,后续迭代再读 pos.id/pos.exchange 会抛
                # MissingGreenlet 中断整批同步(#322 循环异常根因)。
                # 先快照 id/exchange,每轮按 ID 重查拿干净对象。
                for _snap in [(p.id, (p.exchange or "").lower()) for p in positions]:
                    _snap_pos_id, _snap_exchange = _snap
                    if _snap_exchange not in SUPPORTED_EXCHANGES:
                        continue
                    pos = (await db.execute(
                        select(Position).where(Position.id == _snap_pos_id)
                    )).scalar_one_or_none()
                    if pos is None:
                        continue
                    try:
                        await sync_exchange_stop_for_position(db, pos)
                    except Exception as e:
                        logger.warning(
                            f"[交易所止损] 同步异常 pos={_snap_pos_id}: {str(e)[:200]}"
                        )
                        try:
                            await db.rollback()
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"[交易所止损] 同步循环异常: {str(e)[:200]}")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)

