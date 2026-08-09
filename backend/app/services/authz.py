"""时间授权服务:未授权或过期则禁止下单。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Authorization, Customer


async def has_valid_authorization(
    db: AsyncSession, customer_id: int, exchange: str = "all"
) -> bool:
    """检查客户是否有有效授权(exchange='all' 或精确匹配)。"""
    now = datetime.now(timezone.utc)
    stmt = select(Authorization).where(
        Authorization.customer_id == customer_id,
        Authorization.active.is_(True),
        Authorization.starts_at <= now,
        Authorization.expires_at >= now,
    )
    result = await db.execute(stmt)
    auths = result.scalars().all()
    if not auths:
        return False
    # 任一授权覆盖该交易所即可
    for a in auths:
        if a.exchange == "all" or a.exchange == exchange:
            return True
    return False


async def get_authorization_status(
    db: AsyncSession, customer_id: int
) -> dict:
    """返回客户授权状态摘要。"""
    now = datetime.now(timezone.utc)
    stmt = select(Authorization).where(
        Authorization.customer_id == customer_id,
        Authorization.active.is_(True),
    )
    result = await db.execute(stmt)
    auths = result.scalars().all()
    valid = [a for a in auths if a.starts_at <= now <= a.expires_at]
    if valid:
        return {
            "authorized": True,
            "expires_at": max(a.expires_at for a in valid),
            "exchanges": sorted({a.exchange for a in valid}),
        }
    return {"authorized": False, "expires_at": None, "exchanges": []}


async def list_expiring_soon(db: AsyncSession, days: int = 3) -> list[Authorization]:
    """即将到期(用于提前预警)。"""
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=days)
    stmt = select(Authorization).where(
        Authorization.active.is_(True),
        Authorization.expires_at <= threshold,
        Authorization.expires_at >= now,
    )
    result = await db.execute(stmt)
    return result.scalars().all()
