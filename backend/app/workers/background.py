"""后台任务启动器:Discord 监听、持仓监控、待触发单监控、净值快照、授权到期预警、交易所对账。"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.core.database import AsyncSessionLocal
from app.services import analytics, discord_monitor, pending_order_manager, position_manager

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
}

WATCHDOG_INTERVAL = 60  # 看门狗检查间隔(秒)





async def _equity_snapshot_job() -> None:

    """定时对所有客户的每个交易所记录净值快照。



    每个客户用独立 session,单个客户失败(如 API Key 无效)不影响其他客户,

    避免 rollback 后共享 session 损坏导致后续查询抛 MissingGreenlet。

    """

    from sqlalchemy import select



    from app.models.config import ExchangeAccount



    try:

        # 预取 (customer_id, exchange, testnet) 元组,不持有 ORM 对象,避免循环中属性访问触发隐式 IO

        async with AsyncSessionLocal() as db:

            rows = (

                await db.execute(

                    select(ExchangeAccount.customer_id, ExchangeAccount.exchange, ExchangeAccount.testnet)

                    .where(ExchangeAccount.is_active.is_(True))

                    .where(ExchangeAccount.last_error == "")

                    .distinct()

                )

            ).all()

            pairs = [(r.customer_id, r.exchange, r.testnet) for r in rows]



        # 每个客户独立 session,失败隔离

        for cid, ex, testnet in pairs:

            try:

                async with AsyncSessionLocal() as db:

                    await analytics.take_equity_snapshot(db, cid, ex, testnet)

            except Exception as e:

                logger.warning(f"净值快照失败 customer={cid} exchange={ex} testnet={testnet}: {e}")

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
                    .where(ExchangeAccount.last_error == "")
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

                        logger.error(f"后台循环 {name} 已退出,异常={exc},准备重启")

                    new_task = asyncio.create_task(factory(), name=f"loop:{name}")

                    new_task.add_done_callback(

                        lambda t, n=name: logger.error(f"后台循环 {n} 退出: cancelled={t.cancelled()} exc={t.exception() if not t.cancelled() else None}")

                    )

                    _background_tasks[name] = new_task

                    logger.info(f"后台循环 {name} 已(重新)启动")

        except Exception as e:

            logger.exception(f"看门狗检查异常: {e}")

        # ★ 新增: 监控 APScheduler 状态
        if _scheduler and not _scheduler.running:
            logger.error("APScheduler 已停止运行,准备重启")
            try:
                _scheduler.start()
                logger.info("APScheduler 重启成功")
            except Exception as e:
                logger.error(f"APScheduler 重启失败: {e}")
        await asyncio.sleep(WATCHDOG_INTERVAL)





async def start_background_tasks() -> None:

    """应用启动时调用:启动所有后台循环与定时任务。"""

    global _scheduler, _watchdog_task

    def _log_loop_exit(task: asyncio.Task, name: str) -> None:
        if task.cancelled():
            logger.info(f"后台循环 {name} 已取消退出")
            return
        exc = task.exception()
        if exc is None:
            logger.info(f"后台循环 {name} 正常退出")
            return
        logger.error(f"后台循环 {name} 异常退出: {exc}")

    # 后台循环(保留引用防止 GC,并供看门狗监控)

    for name, factory in _LOOP_FACTORIES.items():

        _background_tasks[name] = asyncio.create_task(factory(), name=f"loop:{name}")

        _background_tasks[name].add_done_callback(

            lambda t, n=name: _log_loop_exit(t, n)

        )



    # 定时任务

    _scheduler = AsyncIOScheduler()

    _scheduler.add_job(_equity_snapshot_job, "interval", minutes=5, id="equity_snapshot")
    _scheduler.add_job(_daily_risk_snapshot_job, "interval", minutes=5, id="daily_risk_snapshot")

    _scheduler.add_job(_auth_expire_job, "interval", hours=6, id="auth_expire")

    _scheduler.add_job(_timeout_position_job, "interval", minutes=15, id="timeout_position")

    _scheduler.add_job(_kol_risk_check_job, "interval", minutes=10, id="kol_risk_check")
    _scheduler.add_job(_reconciliation_job, "interval", minutes=10, id="reconciliation")
    _scheduler.start()



    # 看门狗

    _watchdog_task = asyncio.create_task(_watchdog(), name="watchdog")

    logger.info("后台任务已启动(Discord 监听 / 持仓监控 / 待触发单监控 / 止损监控(1秒级) / 净值快照 / 日风控快照 / 授权预警 / 超时平仓 / KOL风控 / 交易所对账(10分钟) / 看门狗)")





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

    # 取消后台循环

    for name, task in list(_background_tasks.items()):

        if not task.done():

            task.cancel()

            try:

                await task

            except (asyncio.CancelledError, Exception):

                pass

        _background_tasks.pop(name, None)

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
