"""风控服务:静默时段、仓位上限、并发上限、单日亏损熔断、连亏暂停、KOL频率限制。"""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import RiskConfig
from app.models.trading import Position, Trade
from app.models.customer import Customer
from app.models.kol import Kol
from app.models.signal import Signal


# 北京时区(UTC+8)
_BEIJING_TZ = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_beijing() -> datetime:
    """返回当前北京时间(UTC+8)。"""
    return datetime.now(_BEIJING_TZ)


def _parse_hhmm(s: str) -> time:
    # M8修复: 添加输入验证,防止格式错误导致风控崩溃
    try:
        parts = s.split(":")
        if len(parts) != 2:
            logger.warning(f"无效的时间格式(非HH:MM): {s}")
            return time(0, 0)
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            logger.warning(f"时间值超出范围: {s}")
            return time(0, 0)
        return time(h, m)
    except (ValueError, IndexError) as e:
        logger.warning(f"时间解析失败 '{s}': {e}")
        return time(0, 0)


def is_in_silent_period(ranges: list[dict[str, Any]], now: datetime) -> bool:
    """判断当前时间是否在静默时段(支持跨午夜,如 23:00-07:00)。

    传入的 ``now`` 视为 UTC 时间,内部转换为北京时间(UTC+8)后再做区间判断。
    """
    from datetime import timedelta
    # 将 UTC 时间转换为北京时间(UTC+8)
    bj_time = now + timedelta(hours=8)
    now = bj_time
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
        # P2-1 修复: 静默时段检查(按北京时间判断)
        # 支持 silent_action: ignore/skip(跳过), delay(延迟), log_only(仅记录)
        if is_in_silent_period(cfg.silent_ranges or [], now):
            if cfg.silent_action in ("ignore", "skip"):
                return False, "当前为静默时段,信号跳过"
            elif cfg.silent_action == "delay":
                # 静默时段内拒绝信号,由上层在静默时段结束后重新触发
                logger.warning(
                    f"客户 {customer_id} 在静默时段内收到信号 {symbol},已拒绝(delay模式)"
                )
                return False, "当前为静默时段,信号已拒绝,将在静默时段结束后允许"
            # log_only: 仅记录日志,允许通过
            logger.info(
                f"客户 {customer_id} 在静默时段内收到信号 {symbol}(log_only模式),允许通过"
            )
        # 4. 并发持仓数(统计所有 open 仓位,包括 master 和子仓位)
        # P2-2 修复: 不再仅统计 master 仓位(parent_id IS NULL),
        # 而是统计所有 open 仓位,防止通过加仓绕过并发上限
        if cfg.max_concurrent_positions > 0:
            # BUG-3 修复: 并发持仓数检查存在 TOCTOU 竞态,多个信号同时到达可突破上限。
            # 使用 PostgreSQL 事务级 advisory lock 锁定 customer+exchange 组合,
            # 串行化同一客户+交易所的并发持仓计数检查,避免竞态突破上限。
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"pos_count_{customer_id}_{exchange}"},
            )
            total_positions = (
                await db.execute(
                    select(func.count(Position.id)).where(
                        Position.customer_id == customer_id,
                        Position.exchange == exchange,
                        Position.status == "open",
                        # FIX: master(parent_id IS NULL)是聚合视图,与子仓位同值;
                        # 只统计子仓位,每笔成交算1个,防止并发上限被腰斩
                        Position.parent_id.is_not(None),
                    )
                )
            ).scalar_one()
            if total_positions >= cfg.max_concurrent_positions:
                return False, f"已达最大并发持仓数 {cfg.max_concurrent_positions}"
        # 5. 单日亏损(含未实现浮亏)
        # P1-4 修复: 不仅统计已实现亏损,还累加所有 open 仓位的未实现浮亏,
        # 防止客户在大量浮亏时仍能开新仓
        if cfg.max_daily_loss_pct > 0:
            from datetime import timedelta
            # 按北京时间(UTC+8)计算自然日
            bj_now = now + timedelta(hours=8)
            bj_midnight = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start = bj_midnight - timedelta(hours=8)  # 转回 UTC
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

            # P1 修复: 累加所有 open 仓位的未实现浮亏
            open_positions = (
                await db.execute(
                    select(Position).where(
                        Position.customer_id == customer_id,
                        Position.exchange == exchange,
                        Position.status == "open",
                        # FIX: master与子仓位qty相同,不过滤会双倍计算浮亏,
                        # 导致单日亏损熔断提前触发(误拒开仓);与
                        # position_manager/analytics 口径一致只算子仓位
                        Position.parent_id.is_not(None),
                    )
                )
            ).scalars().all()

            unrealized_pnl_total = 0.0
            if open_positions:
                # EX-M5 修复: 并行获取持仓价格,避免缓存全 miss 时串行 API 调用导致 2-10 秒延迟
                import asyncio as _aio
                from app.services.position_manager import _get_cached_price, compute_pnl
                from app.services import exchange_adapter

                async def _get_pos_pnl(pos):
                    """获取单个持仓的未实现盈亏。"""
                    try:
                        current_price = await _get_cached_price(pos.exchange, pos.symbol)
                        if not current_price or current_price <= 0:
                            current_price = await exchange_adapter.fetch_market_price(
                                pos.exchange, pos.symbol
                            )
                        if current_price and current_price > 0:
                            pnl, _ = compute_pnl(pos, current_price)
                            return pnl
                    except Exception as e:
                        logger.debug(f"获取仓位 {pos.id} 浮亏失败: {e}")
                    return 0.0

                pnl_results = await _aio.gather(*[_get_pos_pnl(pos) for pos in open_positions])
                unrealized_pnl_total = sum(pnl_results)

            total_daily_loss = daily_pnl + unrealized_pnl_total

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
            base = float(last_eq) if last_eq else 0.0
            # EX-M4 修复: 权益快照不可用时,尝试从交易所获取当前余额作为基准
            if not base:
                try:
                    from app.services import exchange_adapter as _ea
                    ex, _ = await _ea.load_exchange(db, customer_id, exchange)
                    try:
                        bal = await _ea.fetch_balance(ex)
                        base = float(bal.get("equity", 0))
                    finally:
                        await _ea.close_exchange(ex)
                except Exception as eq_err:
                    logger.warning(f"无法获取账户 customer={customer_id} exchange={exchange} 的权益基准: {eq_err}")
            if base > 0:
                if total_daily_loss < 0 and abs(total_daily_loss) / base * 100 >= cfg.max_daily_loss_pct:
                    return False, f"触发单日最大亏损 {cfg.max_daily_loss_pct}%(含浮亏)"
            else:
                # BUG-6 修复: 权益基准为0时(交易所API故障),日亏损熔断不应完全失效。
                # 使用当日已实现亏损与持仓名义价值的比例作为 fallback。
                logger.warning(
                    f"无法获取账户 customer={customer_id} exchange={exchange} 的权益基准,"
                    f"使用持仓名义价值比例作为 fallback 熔断检查"
                )
                # 计算当前持仓总名义价值作为基准
                total_notional = sum(
                    (abs(p.qty or 0) * (p.entry_price or 0)) for p in open_positions
                ) if open_positions else 0.0
                fallback_base = max(total_notional, 1000.0)  # 最低基准1000 USDT
                fallback_loss_pct = cfg.max_daily_loss_pct if cfg.max_daily_loss_pct > 0 else 10.0
                if total_daily_loss < 0 and abs(total_daily_loss) / fallback_base * 100 >= fallback_loss_pct:
                    return False, (
                        f"触发单日亏损熔断(权益基准不可用,当日亏损 "
                        f"{daily_pnl:.2f} USDT 占持仓名义价值 {total_notional:.2f} USDT 的 "
                        f"{abs(total_daily_loss) / fallback_base * 100:.1f}%)"
                    )
    return True, "ok"


# --- 连亏暂停风控 ---

async def check_kol_consecutive_losses(db: AsyncSession) -> None:
    """检查每个 KOL 的连亏情况,达到客户配置阈值时暂停该 KOL 的交易。

    优化: 使用 JOIN 一次性获取 KOL + Follow + RiskConfig,避免 N+1 查询。
    每个客户有独立配置(RiskConfig.consecutive_loss_threshold / pause_hours)。
    阈值=0 表示该客户禁用此风控。
    """
    from app.models.kol import KolFollow
    from app.services.notification import notify

    now = _now()

    # JOIN 查询: KOL + 活跃 Follow + 风控配置 (N+1 -> 1)
    stmt = (
        select(Kol, KolFollow, RiskConfig)
        .join(KolFollow, KolFollow.kol_id == Kol.id)
        .outerjoin(RiskConfig, (
            (RiskConfig.customer_id == KolFollow.customer_id)
            & (RiskConfig.enabled.is_(True))
            & (RiskConfig.exchange == "all")
        ))
        .where(KolFollow.enabled.is_(True))
    )
    rows = (await db.execute(stmt)).all()

    for kol, follow, cfg in rows:
        threshold = cfg.consecutive_loss_threshold if cfg else 5
        pause_hours = cfg.consecutive_loss_pause_hours if cfg else 24

        # 0 = 禁用此风控
        if threshold <= 0:
            continue

        # 获取该客户+KOL 的最近交易结果(按母仓=整笔交易计数)
        # ★ Fix(2026-08-25): 只取母仓行(parent_id IS NULL)。母仓的 realized_pnl
        # 是整笔交易全部子仓盈亏之和,代表该笔交易的最终结果。
        # 旧逻辑按子仓行计数: 一笔交易分批平仓会产生多个子仓,整体盈利的交易
        # 若末批离场子仓为亏(先止盈后止损离场),该亏损子仓仍被计入连亏,盈利
        # 交易无法中断连亏计数(所长 8/20 盈利ENA交易未中断连亏,8/21凌晨被
        # 错误暂停24h);无子仓的整笔交易则被完全漏计。
        recent_trades = (
            await db.execute(
                select(Position).where(
                    Position.kol_id == kol.id,
                    Position.customer_id == follow.customer_id,
                    Position.status == "closed",
                    Position.parent_id.is_(None),
                ).order_by(Position.closed_at.desc()).limit(threshold + 2)
            )
        ).scalars().all()

        if len(recent_trades) < threshold:
            continue

        streak = 0
        for trade in recent_trades[:threshold]:
            # P2 修复: 平本(盈亏=0)计为胜而非亏,只有实际亏损才计入连亏
            if trade.realized_pnl < 0:
                streak += 1
            else:
                break

        if streak >= threshold:
            kol_name = kol.name if kol else "未知KOL"
            # 加行锁防止与 check_kol_can_trade 的自动恢复逻辑竞态
            locked_follow = (await db.execute(
                select(KolFollow).where(KolFollow.id == follow.id).with_for_update()
            )).scalar_one_or_none()
            if not locked_follow or not locked_follow.enabled:
                continue
            locked_follow.enabled = False
            locked_follow.paused_until = now + timedelta(hours=pause_hours)
            # 查找该KOL最近的信号作为溯源
            _risk_src = ""
            try:
                _recent_sig = (await db.execute(
                    select(Signal).where(Signal.kol_id == kol.id).order_by(Signal.id.desc()).limit(1)
                )).scalar_one_or_none()
                if _recent_sig and _recent_sig.raw_text:
                    _risk_src = _recent_sig.raw_text
            except Exception as e:
                logger.debug(f"查询KOL最近信号失败 kol_id={kol.id if kol else None}: {e}")
            # BUG-12 修复: notify 调用加异常保护,飞书 API 故障不应中断后续 KOL 检查
            try:
                await notify(
                    "risk", "KOL 连亏暂停",
                    f"KOL {kol_name} 连续亏损 {streak} 次\n"
                    f"已暂停 {pause_hours} 小时\n"
                    f"预计恢复时间: {locked_follow.paused_until.strftime('%Y-%m-%d %H:%M UTC')}",
                    locked_follow.customer_id,
                    source_text=_risk_src,
                )
            except Exception as notify_err:
                logger.warning(f"KOL 连亏暂停通知发送失败,不影响后续检查: {notify_err}")
            logger.warning(
                f"客户 {locked_follow.customer_id} KOL {kol_name} 连亏 {streak} 次,暂停 {pause_hours}h"
            )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"数据库提交失败: {e}")
        raise

async def check_kol_frequency(db: AsyncSession) -> None:
    """检查 KOL 信号频率,超过客户配置限制时记录告警。

    每个客户有独立配置(RiskConfig.kol_frequency_per_hour)。
    0 = 禁用此风控。
    """
    from app.models.signal import Signal

    one_hour_ago = _now() - timedelta(hours=1)

    # 获取所有客户的风控配置
    configs = (
        await db.execute(
            select(RiskConfig).where(RiskConfig.enabled.is_(True))
        )
    ).scalars().all()

    for cfg in configs:
        limit = cfg.kol_frequency_per_hour
        if limit <= 0:
            continue

        # 该客户的所有 KOL
        from app.models.kol import KolFollow
        kol_ids = (
            await db.execute(
                select(KolFollow.kol_id).where(
                    KolFollow.customer_id == cfg.customer_id,
                    KolFollow.enabled.is_(True),
                )
            )
        ).scalars().all()

        if not kol_ids:
            continue

        # 检查每个 KOL 的信号数
        counts = (
            await db.execute(
                select(Signal.kol_id, func.count(Signal.id).label("cnt"))
                .where(
                    Signal.received_at >= one_hour_ago,
                    Signal.kol_id.in_(kol_ids),
                )
                .group_by(Signal.kol_id)
                .having(func.count(Signal.id) > limit)
            )
        ).all()

        for kol_id, count in counts:
            kol = (await db.execute(select(Kol).where(Kol.id == kol_id))).scalar_one_or_none()
            kol_name = kol.name if kol else f"KOL#{kol_id}"
            logger.warning(
                f"客户 {cfg.customer_id} KOL {kol_name} 过去一小时内收到 {count} 个信号(上限 {limit})"
            )


async def check_kol_can_trade(
    db: AsyncSession, customer_id: int, kol_id: int
) -> tuple[bool, str]:
    """检查 KOL 是否可以交易(连亏暂停、频率限制)。"""
    from app.models.kol import KolFollow

    follow = (
        await db.execute(
            select(KolFollow).where(
                KolFollow.customer_id == customer_id,
                KolFollow.kol_id == kol_id,
            ).with_for_update()
        )
    ).scalar_one_or_none()

    if not follow:
        return False, "客户未关注该 KOL"

    if not follow.enabled:
        if follow.paused_until and follow.paused_until > _now():
            return False, f"该 KOL 已被暂停至 {follow.paused_until.strftime('%Y-%m-%d %H:%M UTC')}"
        elif follow.paused_until:
            # P0-2修复: paused_until 已过期 → 连亏熔断自动暂停到期,允许自动恢复
            follow.enabled = True
            follow.paused_until = None
            # 使用 flush 而非 commit,避免影响调用方事务(advisory lock 等需在同一事务内)
            # 调用方会在自己的事务中统一 commit
            try:
                await db.flush()
            except Exception as e:
                await db.rollback()
                logger.error(f"数据库flush失败: {e}")
                raise
            logger.info(f"KOL {kol_id} 连亏暂停期结束,自动恢复交易")
        else:
            # P0-2修复: enabled=False 且无 paused_until → 用户手动禁用,不自动恢复
            return False, "该KOL已被手动禁用,请在设置中手动启用"

    return True, "ok"


# --- 自动止损增强 ---

async def ensure_position_has_stop_loss(db: AsyncSession) -> int:
    """检查所有 open 仓位,为没有止损的仓位添加默认止损。

    使用客户配置的 auto_stop_loss_pct(0=禁用,默认5%)。
    Returns: 更新的仓位数量
    """
    positions = (
        await db.execute(
            select(Position).where(
                Position.status == "open",
                Position.sl.is_(None) | (Position.sl == 0),
                Position.parent_id.is_not(None),
            )
        )
    ).scalars().all()

    if not positions:
        return 0

    updated = 0
    for pos in positions:
        if pos.sl is None or pos.sl <= 0:
            # 获取客户配置
            cfg = await get_risk_config(db, pos.customer_id, "all")
            sl_pct = (cfg.auto_stop_loss_pct if cfg else 5.0) / 100.0
            # 0 = 禁用
            if sl_pct <= 0:
                continue

            from app.services.signal_filter import stop_loss_price_from_pct

            new_sl = stop_loss_price_from_pct(pos.side, pos.entry_price, sl_pct)
            pos.sl = new_sl
            pos.initial_sl = new_sl
            updated += 1
            logger.info(
                f"自动设置止损 pos={pos.id} symbol={pos.symbol} "
                f"side={pos.side} entry={pos.entry_price} sl={pos.sl} ({sl_pct*100}%)"
            )

    if updated > 0:
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"数据库提交失败: {e}")
            raise
    return updated
