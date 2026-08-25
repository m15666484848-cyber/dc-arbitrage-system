"""后台任务启动器:Discord 监听、持仓监控、待触发单监控、净值快照、授权到期预警、交易所对账。"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.core.database import AsyncSessionLocal
from app.services import analytics, discord_monitor, exchange_stop_manager, pending_order_manager, position_manager

from app.services.authz import list_expiring_soon

from app.services.notification import notify



# 模块级引用:持有 task/scheduler 引用防止 GC,并支持看门狗监控与优雅关闭

_background_tasks: dict[str, asyncio.Task] = {}

_scheduler: AsyncIOScheduler | None = None

_watchdog_task: asyncio.Task | None = None

# 各循环的工厂函数,看门狗用来重启已退出的循环

_LOOP_FACTORIES: dict[str, callable] = {

    "discord": discord_monitor.run_discord_monitor,

    "position_monitor": position_manager.monitor_loop,

    "pending_monitor": pending_order_manager.monitor_loop,

    "stop_loss_monitor": position_manager.stop_loss_monitor_loop,
    "exchange_stop_sync": exchange_stop_manager.exchange_stop_sync_loop,
}

WATCHDOG_INTERVAL = 60  # 看门狗检查间隔(秒)


def _setup_scheduler_jobs(scheduler: AsyncIOScheduler) -> None:
    """注册所有定时任务(replace_existing=True 防止重启后重复添加)。

    S10修复:
    - coalesce=True: 错过的多次执行合并为一次
    - max_instances=1: 防止同一任务并发执行
    - misfire_grace_time=300: 允许5分钟内的迟到执行
    """
    _job_kwargs = {"replace_existing": True, "coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    scheduler.add_job(_equity_snapshot_job, "interval", minutes=5, id="equity_snapshot", **_job_kwargs)
    scheduler.add_job(_daily_risk_snapshot_job, "interval", minutes=5, id="daily_risk_snapshot", **_job_kwargs)
    scheduler.add_job(_auth_expire_job, "interval", hours=6, id="auth_expire", **_job_kwargs)
    scheduler.add_job(_timeout_position_job, "interval", hours=1, id="timeout_position", **_job_kwargs)
    scheduler.add_job(_tpsl_timeout_protection_job, "interval", minutes=30, id="tpsl_timeout_protection", **_job_kwargs)
    scheduler.add_job(_kol_risk_check_job, "interval", minutes=10, id="kol_risk_check", **_job_kwargs)
    scheduler.add_job(_reconciliation_job, "interval", minutes=10, id="reconciliation", **_job_kwargs)
    scheduler.add_job(_data_archival_job, "interval", hours=24, id="data_archival", **_job_kwargs)
    scheduler.add_job(_quiet_digest_job, "cron", hour=21, minute=0, id="quiet_digest", timezone="Asia/Shanghai", **_job_kwargs)





async def _equity_snapshot_job() -> None:

    """定时对所有客户的每个交易所记录净值快照。



    每个客户用独立 session,单个客户失败(如 API Key 无效)不影响其他客户,

    避免 rollback 后共享 session 损坏导致后续查询抛 MissingGreenlet。

    """

    from sqlalchemy import select



    from app.models.config import ExchangeAccount



    try:

        # 预取具体交易所账号,避免 testnet 与 demo 或多 API 账号互相混用。

        async with AsyncSessionLocal() as db:

            rows = (

                await db.execute(

                    select(
                        ExchangeAccount.id,
                        ExchangeAccount.customer_id,
                        ExchangeAccount.exchange,
                        ExchangeAccount.testnet,
                    )

                    .where(ExchangeAccount.is_active.is_(True))

                    .distinct()

                )

            ).all()

            accounts = [(r.id, r.customer_id, r.exchange, r.testnet) for r in rows]



        # 每个客户独立 session,失败隔离

        for account_id, cid, ex, testnet in accounts:

            try:

                async with AsyncSessionLocal() as db:

                    await analytics.take_equity_snapshot(
                        db,
                        cid,
                        ex,
                        testnet,
                        exchange_account_id=account_id,
                    )

            except Exception as e:

                logger.warning(
                    f"净值快照失败 customer={cid} exchange={ex} "
                    f"account_id={account_id} testnet={testnet}: {e}"
                )

    except Exception as e:

        logger.exception(f"净值快照任务异常: {e}")






async def _daily_risk_snapshot_job() -> None:
    """定时生成日风控快照,用于每日亏损、浮亏和熔断阈值追踪。"""
    from sqlalchemy import select

    from app.models.config import ExchangeAccount

    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ExchangeAccount.customer_id, ExchangeAccount.exchange, ExchangeAccount.testnet)
                    .where(ExchangeAccount.is_active.is_(True))
                    .distinct()
                )
            ).all()
            pairs = [(r.customer_id, r.exchange, r.testnet) for r in rows]

        seen_all: set[int] = set()
        for cid, ex, testnet in pairs:
            try:
                async with AsyncSessionLocal() as db:
                    await analytics.take_daily_risk_snapshot(db, cid, ex, testnet=testnet)
            except Exception as e:
                logger.warning(f"日风控快照失败 customer={cid} exchange={ex} testnet={testnet}: {e}")
            if cid not in seen_all:
                seen_all.add(cid)
                try:
                    async with AsyncSessionLocal() as db:
                        await analytics.take_daily_risk_snapshot(db, cid, "all")
                except Exception as e:
                    logger.warning(f"日风控汇总快照失败 customer={cid}: {e}")
    except Exception as e:
        logger.exception(f"日风控快照任务异常: {e}")


async def _quiet_digest_job() -> None:
    """每日21:00(北京时间)推送P2静默事件汇总日报。"""
    try:
        from app.services.notification import send_quiet_digest
        n = await send_quiet_digest()
        if n:
            logger.info(f"静默事件日报已推送: {n}条P2汇总")
    except Exception:
        logger.opt(exception=True).error("静默事件日报任务失败")


async def _auth_expire_job() -> None:

    """授权即将到期预警。"""

    try:

        async with AsyncSessionLocal() as db:

            expiring = await list_expiring_soon(db, days=3)

            for auth in expiring:

                await notify(

                    "auth_expire", "授权即将到期",

                    f"客户ID {auth.customer_id} 的 {auth.exchange} 授权将于 {auth.expires_at} 到期",

                )

    except Exception as e:

        logger.exception(f"授权预警任务异常: {e}")







async def _tpsl_timeout_protection_job() -> None:
    """止盈止损超时分级保护: 每30分钟检查一次。"""
    try:
        async with AsyncSessionLocal() as db:
            processed = await position_manager.check_and_apply_tpsl_timeout_protection(db)
            if processed > 0:
                logger.info(f"止盈止损超时保护任务完成: 处理 {processed} 个持仓")
    except Exception as e:
        logger.exception(f"止盈止损超时保护任务异常: {e}")


async def _timeout_position_job() -> None:

    """超时持仓自动平仓(超过 48h 的持仓)。"""

    try:

        async with AsyncSessionLocal() as db:

            closed = await position_manager.check_and_close_timeout_positions(db)

            if closed > 0:

                logger.info(f"超时平仓任务完成: {closed} 个持仓已自动关闭")

    except Exception as e:

        logger.exception(f"超时平仓任务异常: {e}")





async def _kol_risk_check_job() -> None:

    """KOL 风控检查:连亏暂停 + 频率限制 + 自动止损补充。"""

    try:

        from app.services.risk_manager import (

            check_kol_consecutive_losses,

            check_kol_frequency,

            ensure_position_has_stop_loss,

        )

        async with AsyncSessionLocal() as db:

            await check_kol_consecutive_losses(db)

        async with AsyncSessionLocal() as db:

            await check_kol_frequency(db)

        async with AsyncSessionLocal() as db:

            sl_count = await ensure_position_has_stop_loss(db)

            if sl_count > 0:

                logger.info(f"自动止损: 为 {sl_count} 个仓位补充了默认止损")

    except Exception as e:

        logger.exception(f"KOL 风控检查异常: {e}")





async def _reconciliation_job() -> None:
    """交易所对账:定期比对本地 DB 与交易所实际持仓/挂单,检测并修复差异。

    每 10 分钟执行一次,检测:
      - 幽灵持仓(本地有但交易所无) → 自动标记 closed
      - 孤儿持仓(交易所有但本地无) → 告警
      - 数量不一致 → 告警
      - 幽灵挂单(本地有但交易所无) → 自动标记 cancelled
      - 孤儿挂单(交易所有但本地无) → 告警
    """
    try:
        from app.services.reconciliation import run_reconciliation
        report = await run_reconciliation()
        if report.has_issues:
            logger.warning(
                f"[对账] 发现差异: 持仓 {len(report.position_discrepancies)} 项, "
                f"挂单 {len(report.order_discrepancies)} 项, "
                f"自动修复 {report.auto_fixed} 项"
            )
    except Exception as e:
        logger.exception(f"交易所对账任务异常: {e}")




async def _data_archival_job() -> None:
    """S10新增: 数据归档任务 - 清理超过90天的旧数据,保持数据库性能。

    清理范围:
    - equity_snapshots: 保留90天(高频写入,增长最快)
    - alert_logs: 保留90天
    - audit_logs: 保留180天(审计需要更长保留期)
    - trades: 保留180天(成交流水,可能需要用于统计)
    """
    from sqlalchemy import delete, text
    from datetime import datetime, timedelta, timezone

    from app.models import EquitySnapshot, AlertLog
    from app.models.audit import AuditLog
    from app.models.trading import Trade

    cutoff_90 = datetime.now(timezone.utc) - timedelta(days=90)
    cutoff_180 = datetime.now(timezone.utc) - timedelta(days=180)

    try:
        async with AsyncSessionLocal() as db:
            # 清理旧净值快照(高频写入,增长最快)
            result1 = await db.execute(
                delete(EquitySnapshot).where(EquitySnapshot.snapshot_at < cutoff_90)
            )
            # 清理旧告警日志
            result2 = await db.execute(
                delete(AlertLog).where(AlertLog.created_at < cutoff_90)
            )
            # 清理旧审计日志(保留180天)
            result3 = await db.execute(
                delete(AuditLog).where(AuditLog.created_at < cutoff_180)
            )
            # 清理旧成交流水(保留180天)
            result4 = await db.execute(
                delete(Trade).where(Trade.executed_at < cutoff_180)
            )
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                raise

            total = result1.rowcount + result2.rowcount + result3.rowcount + result4.rowcount
            if total > 0:
                logger.info(
                    f"[数据归档] 清理完成: equity_snapshots={result1.rowcount}, "
                    f"alert_logs={result2.rowcount}, audit_logs={result3.rowcount}, "
                    f"trades={result4.rowcount}"
                )
    except Exception as e:
        await db.rollback()
        logger.exception(f"数据归档任务异常: {e}")

async def _watchdog() -> None:

    """看门狗:定期检查后台循环是否存活,意外退出则自动重启。



    各循环内部已有 while True + try/except 自愈,但仍可能因未捕获异常或任务被取消而退出。

    看门狗作为第二道防线确保关键循环持续运行。

    """

    logger.info("后台任务看门狗已启动")

    while True:

        try:

            for name, factory in _LOOP_FACTORIES.items():

                task = _background_tasks.get(name)

                if task is None or task.done():

                    if task is not None:

                        # 记录退出原因

                        exc = task.exception() if not task.cancelled() else None

                        logger.info(f"后台循环 {name} 已退出(正常取消),准备重启") if exc is None else logger.error(f"后台循环 {name} 异常退出,异常={exc},准备重启")

                    new_task = asyncio.create_task(factory(), name=f"loop:{name}")

                    new_task.add_done_callback(

                        lambda t, n=name: logger.info(f"后台循环 {n} 退出: cancelled={t.cancelled()} exc={t.exception() if not t.cancelled() else None}") if t.cancelled() else logger.error(f"后台循环 {n} 异常退出: exc={t.exception()}")

                    )

                    _background_tasks[name] = new_task

                    logger.info(f"后台循环 {name} 已(重新)启动")

        except Exception as e:

            logger.exception(f"看门狗检查异常: {e}")

        # ★ 新增: 监控 APScheduler 状态
        if _scheduler and not _scheduler.running:
            logger.error("APScheduler 已停止运行,准备重启")
            try:
                # S6修复: 重启时重新注册定时任务,防止任务丢失
                _setup_scheduler_jobs(_scheduler)
                _scheduler.start()
                logger.info("APScheduler 重启成功,定时任务已重新注册")
            except Exception as e:
                logger.error(f"APScheduler 重启失败: {e}")
        await asyncio.sleep(WATCHDOG_INTERVAL)





async def start_background_tasks() -> None:

    """应用启动时调用:启动所有后台循环与定时任务。"""

    
    # 启动时加载币种分类缓存
    try:
        from app.services.signal_filter import refresh_coin_tier_cache
        await refresh_coin_tier_cache()
        logger.info("币种分类缓存已加载")
    except Exception as e:
        logger.warning(f"加载币种分类缓存失败: {e}")

    global _scheduler, _watchdog_task

    # 后台循环(保留引用防止 GC,并供看门狗监控)

    for name, factory in _LOOP_FACTORIES.items():

        _background_tasks[name] = asyncio.create_task(factory(), name=f"loop:{name}")

        _background_tasks[name].add_done_callback(

            lambda t, n=name: logger.info(f"后台循环 {n} 退出: cancelled={t.cancelled()} exc={t.exception() if not t.cancelled() else None}") if t.cancelled() else logger.error(f"后台循环 {n} 异常退出: exc={t.exception()}")

        )



    # 定时任务

    _scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )

    _setup_scheduler_jobs(_scheduler)

    _scheduler.start()



    # 看门狗

    _watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

    logger.info("后台任务已启动(Discord 监听 / 持仓监控 / 待触发单监控 / 止损监控(1秒级) / 净值快照 / 日风控快照 / 授权预警 / 超时平仓 / KOL风控 / 交易所对账(10分钟) / 数据归档(24小时) / 看门狗)")





async def stop_background_tasks() -> None:

    """应用关闭时调用:优雅停止所有后台循环与定时任务。"""

    global _scheduler, _watchdog_task

    # 停止看门狗

    if _watchdog_task and not _watchdog_task.done():

        _watchdog_task.cancel()

        try:

            await _watchdog_task

        except (asyncio.CancelledError, Exception):

            pass

    # 停止定时任务

    if _scheduler:

        try:

            _scheduler.shutdown(wait=False)

        except Exception as e:

            logger.warning(f"停止定时任务异常: {e}")

        _scheduler = None

    # P0修复: 取消后台循环时添加5秒宽限期
    # 止损监控循环可能在平仓操作中途,直接cancel会导致交易所侧已下单但本地未记录
    for name, task in list(_background_tasks.items()):

        if not task.done():

            task.cancel()

            try:
                # 给予5秒宽限期让关键操作(平仓/下单)完成
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)

            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                # 宽限期结束后强制取消
                if not task.done():
                    logger.warning(f"后台循环 {name} 5秒宽限期后仍在运行,强制取消")
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        _background_tasks.pop(name, None)

    # S11新增: 清理公开行情交易所实例
    try:
        from app.services.exchange_adapter import close_all_public_exchanges
        await close_all_public_exchanges()
    except Exception as e:
        logger.warning(f"清理公开行情交易所实例失败: {e}")

    logger.info("后台任务已全部停止")





def get_background_tasks_status() -> dict[str, dict]:

    """返回各后台循环的存活状态(供深度健康检查使用)。"""

    status: dict[str, dict] = {}

    for name in _LOOP_FACTORIES:

        task = _background_tasks.get(name)

        if task is None:

            status[name] = {"alive": False, "reason": "未启动"}

        elif task.done():

            exc = task.exception() if not task.cancelled() else "cancelled"

            status[name] = {"alive": False, "reason": f"已退出: {exc}"}

        else:

            status[name] = {"alive": True}

    if _watchdog_task and not _watchdog_task.done():

        status["watchdog"] = {"alive": True}

    return status

