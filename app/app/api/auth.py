"""认证路由:登录、当前用户信息。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.models.customer import Customer
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.common import ok
from app.services.authz import get_authorization_status

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    # 先查管理员,再查客户
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    role = "admin"
    if not user:
        cust = (await db.execute(select(Customer).where(Customer.username == body.username))).scalar_one_or_none()
        if not cust:
            raise HTTPException(401, "用户名或密码错误")
        if not verify_password(body.password, cust.password_hash):
            raise HTTPException(401, "用户名或密码错误")
        if not cust.is_active:
            raise HTTPException(403, "账号已禁用")
        cust.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        auth_status = await get_authorization_status(db, cust.id)
        token = create_access_token(cust.username, "customer", {"customer_id": cust.id})
        return TokenResponse(
            access_token=token, role="customer", user_id=cust.id,
            username=cust.username, display_name=cust.display_name,
        )
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    token = create_access_token(user.username, "admin", {"user_id": user.id})
    return TokenResponse(
        access_token=token, role="admin", user_id=user.id,
        username=user.username, display_name=user.username,
    )


@router.get("/me")
async def me(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    data = {
        "id": current.id,
        "username": current.username,
        "role": current.role,
        "is_active": current.is_active,
    }
    if current.role == "customer":
        auth_status = await get_authorization_status(db, current.id)
        data["display_name"] = current.display_name
        data["authorization"] = auth_status
    return ok(data)
