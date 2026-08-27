"""SSO服务端点: 主授权服务器(pro.dclh.net)换取客户token。

设计: 主服务器的admin给用户分配 ai_trading license后, 客户端用主账号JWT
调用主服务器的 /api/AiTradingToken, 主服务器再调用本端点换取DC QUANT token。
本端点以服务密钥认证(非用户密码), 自动建户(register_source='sso')并同步授权期限,
保证主服务器是授权唯一来源。
"""
from datetime import datetime, timezone
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password
from app.models.customer import Authorization, Customer
from app.models.config import ExchangeAccount
from app.models.kol import Kol, KolFollow
from app.services.authz import get_authorization_status

# SSO 自动开通账户的默认关注 KOL
DEFAULT_FOLLOW_KOL_NAMES = ("飞扬", "所长", "舒琴", "陈哥", "峰哥")


async def _ensure_default_follows(db: AsyncSession, cust: Customer, is_new: bool) -> None:
    """新账户自动关注默认 KOL;已有账户仅在从未配置过关注时补一次。

    已有任何关注记录的账户不再补,避免覆盖客户主动取消的关注。
    """
    existing = set(
        (await db.execute(select(KolFollow.kol_id).where(KolFollow.customer_id == cust.id)))
        .scalars()
        .all()
    )
    if existing and not is_new:
        return
    kol_ids = (
        (await db.execute(
            select(Kol.id).where(Kol.name.in_(DEFAULT_FOLLOW_KOL_NAMES), Kol.enabled.is_(True))
        ))
        .scalars()
        .all()
    )
    added = 0
    for kid in kol_ids:
        if kid not in existing:
            db.add(KolFollow(customer_id=cust.id, kol_id=kid, enabled=True))
            added += 1
    if added:
        logger.info(f"SSO账户 {cust.username} 默认关注 {added} 个KOL: {DEFAULT_FOLLOW_KOL_NAMES}")


router = APIRouter(prefix="/sso", tags=["SSO"])


class SsoTokenRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    display_name: str = ""
    expires_at: datetime
    max_order_usdt: float = Field(default=5000.0, ge=0, le=99999)
    # 下单模式: fixed=固定金额(策略基准x倍率) | equity_pct=资金比例(账户权益x百分比)
    # None=主服务器未指定,不覆盖客户已有设置(PRO 端 DcqSsoService 不传这两个字段)
    position_mode: str | None = Field(default=None, pattern="^(fixed|equity_pct|fixed_amount)$")
    position_pct: float | None = None


class SsoTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "customer"
    user_id: int
    username: str
    display_name: str = ""


def _require_sso_key(x_sso_key: str = Header(default="")) -> None:
    if not settings.sso_service_key or x_sso_key != settings.sso_service_key:
        raise HTTPException(401, "SSO服务密钥无效")


def _validate_sso_username(name: str) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not name or any(ch not in allowed for ch in name):
        raise HTTPException(400, "SSO用户名仅允许字母/数字/下划线/横线")


@router.post("/token", dependencies=[Depends(_require_sso_key)])
async def sso_token(body: SsoTokenRequest, db: AsyncSession = Depends(get_db)):
    _validate_sso_username(body.username)
    now = datetime.now(timezone.utc)
    if body.expires_at.tzinfo is None:
        body.expires_at = body.expires_at.replace(tzinfo=timezone.utc)

    cust = (
        await db.execute(select(Customer).where(Customer.username == body.username))
    ).scalar_one_or_none()

    if cust is None:
        cust = Customer(
            username=body.username,
            password_hash=hash_password(secrets.token_urlsafe(24)),
            # 登录名已含主服务器用户名(dclh_{uid}_{username});显示名直接用主服务器显示名
            display_name=body.display_name or body.username,
            is_active=True,
            status="active",
            register_source="sso",
            note="主服务器SSO自动开通",
            max_order_usdt=body.max_order_usdt,
        )
        db.add(cust)
        await db.flush()
        is_new = True
    else:
        is_new = False
        if cust.status not in ("active",):
            cust.status = "active"
        if not cust.is_active:
            cust.is_active = True
        if body.display_name and cust.display_name != body.display_name:
            cust.display_name = body.display_name
        # 管理员单笔上限同步: 主服务器(PRO)始终携带该值,是唯一配置来源
        if cust.max_order_usdt != body.max_order_usdt:
            cust.max_order_usdt = body.max_order_usdt
            logger.info(
                f"SSO账户 {cust.username} 管理员单笔上限同步: {body.max_order_usdt} USDT"
            )

    auth = (
        await db.execute(
            select(Authorization).where(
                Authorization.customer_id == cust.id,
                Authorization.exchange == "all",
                Authorization.note == "SSO",
            )
        )
    ).scalar_one_or_none()
    if auth is None:
        db.add(
            Authorization(
                customer_id=cust.id,
                exchange="all",
                starts_at=now,
                expires_at=body.expires_at,
                active=True,
                note="SSO",
            )
        )
    else:
        auth.starts_at = now
        auth.expires_at = body.expires_at
        auth.active = True

    await _ensure_default_follows(db, cust, is_new)

    # SSO 下单模式同步: 仅主服务器明确传值时同步;未传(None)时保留客户在客户端的设置
    if body.position_mode is not None:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(ExchangeAccount)
            .where(ExchangeAccount.customer_id == cust.id)
            .values(
                position_mode=body.position_mode,
                position_pct=max(0.0, min(100.0, body.position_pct or 0.0)),
            )
        )
        logger.info(
            f"SSO账户 {cust.username} 下单模式同步: {body.position_mode} "
            f"pct={body.position_pct}"
        )

    cust.last_login_at = now
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(500, "SSO登录失败,请稍后重试")
    await db.refresh(cust)

    token = create_access_token(cust.username, "customer", {"customer_id": cust.id})
    auth_status = await get_authorization_status(db, cust.id)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "access_token": token,
            "token_type": "bearer",
            "role": "customer",
            "user_id": cust.id,
            "username": cust.username,
            "display_name": cust.display_name,
            "authorization": auth_status,
        },
    }
