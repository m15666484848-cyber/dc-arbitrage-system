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
from app.models.trading import Position
from app.services.event_bus import bus
from app.services.notification import notify

# 入场价偏离市价超过此阈值(0.1%)时,创建待触发单而非直接市价下单
ENTRY_DEVIATION_THRESHOLD = 0.001

# 默认过期时间(7天)
DEFAULT_EXPIRE_DAYS = 7

# 每客户最多待触发单数(防滥用)
MAX_PENDING_PER_CUSTOMER = 50

# 同客户同品种同方向的 pending 单去重价格容差。
# 只拦截同价/极近价,允许 65000、65500、66200 这类不同价位分批挂单共存。
PENDING_ENTRY_DEDUP_ABS_TOLERANCE = 1e-6
PENDING_ENTRY_DEDUP_REL_TOLERANCE = 0.0005  # 0.05%


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_same_pending_entry_price(existing_price: float | None, new_price: float | None) -> bool:
    """判断两个 pending 入场价是否应视为同一价位。"""
    if existing_price is None or new_price is None:
        return False
    existing = float(existing_price)
    new = float(new_price)
    diff = abs(existing - new)
    if diff <= PENDING_ENTRY_DEDUP_ABS_TOLERANCE:
        return True
    base = max(abs(existing), abs(new), 1.0)
    return diff / base <= PENDING_ENTRY_DEDUP_REL_TOLERANCE


def _format_tp_sl(tp_levels: list | None, sl: float | None) -> tuple[str, str]:
    """格式化止盈止损为可读字符串。"""
    tp_str = "无"
    if tp_levels:
        tp_parts = []
        for tp in tp_levels:
            level = tp.get("level", "?") if isinstance(tp, dict) else "?"
            price = tp.get("price", "") if isinstance(tp, dict) else tp
            pct = tp.get("pct", 0) if isinstance(tp, dict) else 0
            if pct:
                tp_parts.append(f"  TP{level}: {price} ({pct * 100:.0f}%)")
            else:
                tp_parts.append(f"  TP{level}: {price}")
        tp_str = "\n".join(tp_parts)
    sl_str = f"{sl}" if sl else "无"
    return tp_str, sl_str


async def _get_kol_name(db: AsyncSession, kol_id: int | None) -> str:
    """查询 KOL 名称。"""
    if not kol_id:
        return "未知"
    from app.models.kol import Kol
    kol = await db.get(Kol, kol_id)
    return kol.name if kol else "未知"


def _side_cn(side: str) -> str:
    """方向转中文。"""
    if side == "long":
        return "做多(long)"
    elif side == "short":
        return "做空(short)"
    return side or "未知"



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



async def _get_signal_text(db, signal_id: int | None) -> str:
    """根据 signal_id 查询原始消息文本(用于告警溯源)。"""
    if not signal_id:
        return ""
    try:
        from app.models.signal import Signal
        sig = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
        return sig.raw_text if sig else ""
    except Exception as e:
        logger.debug(f"查询待触发单原始信号失败 signal_id={signal_id}: {e}")
        return ""

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
    exchange_account_id: int | None = None,
    strategy_id: int | None = None,
    batch_no: int | None = None,
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

    # 检查同品种同方向是否已有 pending 单。
    # 新策略:同客户/同交易所/同品种/同方向的不同价位允许共存;
    # 只有同一信号同批次,或同价/极近价,才拦截为重复 pending。
    existing_orders = (
        await db.execute(
            select(PendingOrder).where(
                PendingOrder.customer_id == customer_id,
                PendingOrder.exchange == exchange,
                PendingOrder.exchange_account_id == exchange_account_id,
                PendingOrder.symbol == parsed.symbol,
                PendingOrder.side == parsed.side,
                PendingOrder.status == "pending",
            )
        )
    ).scalars().all()
    for existing in existing_orders:
        existing_params = existing.strategy_params or {}
        same_signal = bool(signal_id and existing.signal_id == signal_id)
        same_entry = _is_same_pending_entry_price(existing.entry_price, parsed.entry_price)
        same_batch = batch_no is not None and existing_params.get("batch_no") == batch_no
        if same_signal and same_batch:
            reason = (
                f"同一信号同批次已存在待触发单: existing_id={existing.id} "
                f"batch_no={batch_no} existing_entry={existing.entry_price} new_entry={parsed.entry_price}"
            )
            logger.info(reason)
            return {
                "ok": False,
                "reason": reason,
                "existing_pending_id": existing.id,
                "existing_entry_price": existing.entry_price,
                "new_entry_price": parsed.entry_price,
            }
        if same_entry:
            reason = (
                f"同价位待触发单已存在: existing_id={existing.id} "
                f"signal_id={existing.signal_id} existing_entry={existing.entry_price} "
                f"new_entry={parsed.entry_price} symbol={parsed.symbol} side={parsed.side}"
            )
            logger.info(reason)
            return {
                "ok": False,
                "reason": reason,
                "existing_pending_id": existing.id,
                "existing_entry_price": existing.entry_price,
                "new_entry_price": parsed.entry_price,
            }

    # 构建止盈配置(基于入场价)
    tp_levels = order_manager._build_tp_levels(parsed, defaults, parsed.entry_price, parsed.side)

    pending = PendingOrder(
        customer_id=customer_id,
        kol_id=kol_id,
        signal_id=signal_id,
        exchange_account_id=exchange_account_id,
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
            "batch_no": batch_no,
        },
        status="pending",
        expires_at=_utcnow() + timedelta(days=expire_days),
    )
    db.add(pending)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"数据库提交失败: {e}")
        raise
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
    kol_name = await _get_kol_name(db, pending.kol_id)
    tp_str, sl_str = _format_tp_sl(pending.tp_levels, pending.sl)
    _src_text = await _get_signal_text(db, pending.signal_id)
    await notify(
        "order", "待触发单已创建",
        f"KOL: {kol_name}\n"
        f"交易所: {pending.exchange.upper()}\n"
        f"API账户: #{pending.exchange_account_id or '未知'}\n"
        f"挂单ID: {pending.id}\n"
        f"品种: {pending.symbol}\n方向: {_side_cn(pending.side)}\n"
        f"目标入场价: {pending.entry_price}\n"
        f"止盈:\n{tp_str}\n止损: {sl_str}\n"
        f"杠杆: {pending.leverage}x\n名义价值: {pending.notional_usdt} USDT\n"
        f"过期时间: {pending.expires_at}\n"
        f"执行结果: 已创建待触发单,尚未进场\n"
        f"执行依据: KOL 入场价与当前市价偏离超过阈值,先等待价格触及目标价后再市价进场",
        customer_id,
        source_text=_src_text,
    )

    return {"ok": True, "pending_id": pending.id, "batch_no": batch_no}


async def trigger_pending_order(db: AsyncSession, pending: PendingOrder, trigger_price: float | None = None) -> dict:
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
                ExchangeAccount.id == pending.exchange_account_id,
                ExchangeAccount.customer_id == pending.customer_id,
                ExchangeAccount.is_active.is_(True),
            )
            .order_by(
                ExchangeAccount.is_default.desc(),
                ExchangeAccount.last_error.asc(),
                ExchangeAccount.last_verified_at.desc().nullslast(),
                ExchangeAccount.id,
            )
            .with_for_update()
        )
    ).scalars().first()
    if not ex_acc:
        ex_acc = (
            await db.execute(
                select(ExchangeAccount).where(
                    ExchangeAccount.customer_id == pending.customer_id,
                    ExchangeAccount.exchange == pending.exchange,
                    ExchangeAccount.is_active.is_(True),
                )
                .order_by(
                    ExchangeAccount.is_default.desc(),
                    ExchangeAccount.last_error.asc(),
                    ExchangeAccount.last_verified_at.desc().nullslast(),
                    ExchangeAccount.id,
                )
                .with_for_update()
            )
        ).scalars().first()
    if ex_acc and ex_acc.last_error:
        pending.status = "cancelled"
        pending.cancel_reason = (
            f"默认下单 API 验证失败,取消触发: "
            f"{ex_acc.exchange.upper()} {'测试网' if ex_acc.testnet else '实盘'}"
        )
        await db.commit()
        return {"ok": False, "reason": pending.cancel_reason}
    testnet = ex_acc.testnet if ex_acc else False

    # 并发锁: 使用稳定哈希生成 advisory lock key,避免 Python 内置 hash()
    # 因 PYTHONHASHSEED 随机化导致重启/多进程后锁键变化。
    from sqlalchemy import text as _text
    import hashlib as _hashlib
    _lock_src = f"{pending.customer_id}|{pending.exchange_account_id or 0}|{pending.symbol}|{pending.side}"
    _lock_key = (int(_hashlib.md5(_lock_src.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1
    try:
        _lock_acquired = (await db.execute(
            _text("SELECT pg_try_advisory_xact_lock(:key)").bindparams(key=_lock_key)
        )).scalar()
        if not _lock_acquired:
            logger.info(f"待触发单被并发锁阻止: cid={pending.customer_id} {pending.symbol} {pending.side}")
            return {"ok": False, "reason": "同一品种方向正在处理中"}
    except Exception as e:
        logger.warning(f"advisory lock 获取失败(降级为无锁): {e}")

    # 调用 _place_entry 下单
    # ★ 急停检查: 急停状态下不允许触发待触发单
    from app.models.user import User
    customer = await db.get(User, pending.customer_id)
    if customer and getattr(customer, "emergency_stop", False):
        pending.status = "cancelled"
        pending.cancel_reason = "客户急停已激活,待触发单自动取消"
        await db.commit()
        logger.warning(f"待触发单 {pending.id} 因客户急停自动取消")
        return {"ok": False, "reason": "客户急停已激活,待触发单已取消"}

    try:
        result = await order_manager._place_entry(
            db,
            customer_id=pending.customer_id,
            kol_id=pending.kol_id,
            signal_id=pending.signal_id,
            exchange=pending.exchange,
            testnet=testnet,
            exchange_account_id=ex_acc.id if ex_acc else pending.exchange_account_id,
            parsed=parsed,
            notional_usdt=pending.notional_usdt,
            defaults=defaults,
            market_price=pending.entry_price,
            strategy=strategy,
        )
    except Exception as e:
        logger.exception(f"触发待触发单 {pending.id} 下单失败: {e}")
        err_msg = str(e)
        err_low = err_msg.lower()
        # 不可恢复的错误:直接标记为cancelled,防止监控循环无限重试并持续告警。
        # 包括余额不足、最小下单额/精度不足、OKX非双向持仓模式等人工配置类问题。
        non_retriable = any(token in err_low for token in (
            "余额不足", "insufficient",
            "insufficient balance", "insufficient margin",
            "minimum amount", "minimum amount precision",
            "minimum order", "min size",
            "notional must be no smaller", "订单参数无效",
            "订单查询连续失败", "fetchorder() can only access",
            "position side does not match", "-4061",
            "不是双向持仓模式", "long_short_mode",
            "min notional", "min_notional",
            "order value too small", "below minimum",
            "小单被拒",
            "invalid symbol", "无效交易对", "bad symbol",
            "precision over limit", "lot size",
            "market is closed", "交易暂停", "market not found",
            "no enough balance", "margin insufficient",
            "position size is zero", "no position",
            "order rejected", "order failed",
            "leverage not changed", "杠杆设置失败",
            "api key not found", "apikey invalid",
            "signature", "签名错误",
            "permission denied", "权限不足",
            "ip not in whitelist", "ip限制",
            "account suspended", "账户冻结",
        ))
        if non_retriable:
            logger.warning(f"待触发单 {pending.id} 因不可恢复错误自动取消,防止循环触发: {err_msg}")
            pending.status = "cancelled"
            pending.cancel_reason = f"下单失败(不可恢复): {err_msg}"
            try:
                await db.commit()
            except Exception as commit_err:
                logger.error(f"待触发单 {pending.id} 标记取消提交失败: {commit_err}")
                await db.rollback()
        kol_name = await _get_kol_name(db, pending.kol_id)
        tp_str, sl_str = _format_tp_sl(pending.tp_levels, pending.sl)
        _src_text = await _get_signal_text(db, pending.signal_id)
        await notify(
            "error", "待触发单下单失败",
            f"KOL: {kol_name}\n"
            f"交易所: {pending.exchange.upper()}\n"
            f"API账户: #{pending.exchange_account_id or '未知'}\n"
            f"挂单ID: {pending.id}\n"
            f"品种: {pending.symbol}\n方向: {_side_cn(pending.side)}\n"
            f"目标入场价: {pending.entry_price}\n"
            f"触发市价: {order_manager._fmt_value(trigger_price)}\n"
            f"止盈: {tp_str}\n止损: {sl_str}\n"
            f"杠杆: {pending.leverage}x\n名义价值: {pending.notional_usdt} USDT\n"
            f"执行结果: 未创建持仓\n"
            f"失败原因: {e}\n"
            f"判断依据: {order_manager._failure_hint(e)}",
            pending.customer_id,
            source_text=_src_text,
        )
        return {"ok": False, "reason": f"下单异常: {e}"}

    # 标记持仓来源为待触发单触发(不计入冷却)
    _triggered_pos_id = result.get("position_id")
    if _triggered_pos_id:
        _pos = await db.get(Position, _triggered_pos_id)
        if _pos:
            _pos.source = "pending_trigger"

    # 更新待触发单状态
    pending.status = "triggered"
    pending.triggered_at = _utcnow()
    pending.triggered_order_id = result.get("order_id")
    pending.triggered_position_id = result.get("position_id")
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"数据库提交失败: {e}")
        raise

    logger.info(
        f"待触发单 {pending.id} 已触发 order={result.get('order_id')} "
        f"position={result.get('position_id')}"
    )

    await bus.publish_customer(
        pending.customer_id, "pending_order",
        {"id": pending.id, "status": "triggered",
         "order_id": result.get("order_id"), "position_id": result.get("position_id")},
    )
    kol_name = await _get_kol_name(db, pending.kol_id)
    tp_str, sl_str = _format_tp_sl(pending.tp_levels, pending.sl)
    _src_text = await _get_signal_text(db, pending.signal_id)
    await notify(
        "order", "挂单触发进场成功",
        f"KOL: {kol_name}\n"
        f"交易所: {pending.exchange.upper()}\n"
        f"品种: {pending.symbol}\n方向: {_side_cn(pending.side)}\n"
        f"挂单ID: {pending.id}\n"
        f"{order_manager._order_success_lines(action='挂单触发后市价进场成功', order=result.get('order'), requested_entry=pending.entry_price, trigger_price=trigger_price, notional_usdt=pending.notional_usdt, account_id=result.get('exchange_account_id') or pending.exchange_account_id, basis='监控价已触及目标入场价,触发后按市价单执行', exchange=pending.exchange)}\n"
        f"止盈:\n{tp_str}\n止损: {sl_str}\n"
        f"杠杆: {pending.leverage}x\n"
        f"持仓ID: {result.get('position_id')}\n订单ID: {result.get('order_id')}",
        pending.customer_id,
        source_text=_src_text,
    )

    return {"ok": True, "order_id": result.get("order_id"), "position_id": result.get("position_id")}


async def cancel_pending_order(db: AsyncSession, pending_id: int, customer_id: int, reason: str = "") -> dict:
    """手动取消待触发单。"""
    pending = (
        await db.execute(
            select(PendingOrder).where(
                PendingOrder.id == pending_id,
                PendingOrder.customer_id == customer_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not pending:
        return {"ok": False, "reason": "待触发单不存在"}
    if pending.status != "pending":
        return {"ok": False, "reason": f"待触发单状态为 {pending.status},不可取消"}

    pending.status = "cancelled"
    pending.cancel_reason = reason or "用户手动取消"
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"数据库提交失败: {e}")
        raise

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
            ).with_for_update()
        )
    ).scalars().all()

    # 先提取通知所需信息,再commit,最后发通知
    notify_list = []
    for pending in expired:
        pending.status = "expired"
        pending.cancel_reason = "已过期"
        notify_list.append(pending)

    if expired:
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"数据库提交失败: {e}")
            raise
        logger.info(f"清理了 {len(expired)} 个过期待触发单")
        # commit 成功后再发通知,避免 commit 失败时发出虚假通知
        for pending in notify_list:
            try:
                kol_name = await _get_kol_name(db, pending.kol_id)
                tp_str, sl_str = _format_tp_sl(pending.tp_levels, pending.sl)
                _src_text = await _get_signal_text(db, pending.signal_id)
                await notify(
                    "order", "待触发单已过期",
                    f"KOL: {kol_name}\n"
                    f"交易所: {pending.exchange.upper()}\n"
                    f"品种: {pending.symbol}\n方向: {_side_cn(pending.side)}\n"
                    f"目标入场价: {pending.entry_price}\n"
                    f"止盈: {tp_str}\n止损: {sl_str}\n"
                    f"杠杆: {pending.leverage}x\n名义价值: {pending.notional_usdt} USDT\n"
                    f"已自动取消",
                    pending.customer_id,
                    source_text=_src_text,
                )
            except Exception as notify_err:
                logger.warning(f"过期通知发送失败 pending={pending.id}: {notify_err}")

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
                            # 使用独立 session,避免 trigger_pending_order 内部的
                            # commit/rollback 影响 monitor_loop 的 session
                            async with AsyncSessionLocal() as trigger_db:
                                # 在新 session 中重新查询并加锁
                                p = (await trigger_db.execute(
                                    select(PendingOrder).where(
                                        PendingOrder.id == pending.id
                                    ).with_for_update()
                                )).scalar_one_or_none()
                                if p and p.status == "pending":
                                    await trigger_pending_order(trigger_db, p, current_price)
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
            "exchange_account_id": p.exchange_account_id,
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
