"""后台任务启动器:Discord 监听、持仓监控、待触发单监控、净值快照、授权到期预警。"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from app.core.database import AsyncSessionLocal
from app.services import analytics, discord_monitor, pending_order_manager, position_manager
from app.services.authz import list_expiring_soon
from app.services.notification import notify


async def _equity_snapshot_job() -> None:
    """定时对所有客户的每个交易所记录净值快照。"""
    from sqlalchemy import select

    from app.models.config import ExchangeAccount

    try:
        async with AsyncSessionLocal() as db:
            accounts = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.is_active.is_(True)))).scalars().all()
            seen = set()
            for acc in accounts:
                cid = acc.customer_id
                ex = acc.exchange
                key = (cid, ex)
                if key in seen:
                    continue
                seen.add(key)
                await analytics.take_equity_snapshot(db, cid, ex)
    except Exception as e:
        logger.exception(f"净值快照任务异常: {e}")


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


async def start_background_tasks() -> None:
    """应用启动时调用:启动所有后台循环与定时任务。"""
    # 后台循环
    asyncio.create_task(discord_monitor.run_discord_monitor())
    asyncio.create_task(position_manager.monitor_loop())
    asyncio.create_task(pending_order_manager.monitor_loop())

    # 定时任务
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_equity_snapshot_job, "interval", minutes=5, id="equity_snapshot")
    scheduler.add_job(_auth_expire_job, "interval", hours=6, id="auth_expire")
    scheduler.start()
    logger.info("后台任务已启动(Discord 监听 / 持仓监控 / 待触发单监控 / 净值快照 / 授权预警)")
