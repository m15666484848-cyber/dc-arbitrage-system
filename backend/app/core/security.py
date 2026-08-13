"""安全模块:密码哈希、JWT、RBAC 依赖、Fernet 加密。

使用 bcrypt 直接替代 passlib,避免版本兼容性问题。
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import bcrypt as _bcrypt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

Role = Literal["admin", "customer"]


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(subject: str, role: Role, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except JWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期") from e



def create_refresh_token(subject: str, role: Role, extra: dict | None = None) -> str:
    """创建刷新令牌(较长有效期),存储在HttpOnly Cookie中。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_refresh_token(token: str) -> dict:
    """解码刷新令牌,验证类型为refresh。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        if payload.get("type") != "refresh":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的刷新令牌")
        return payload
    except JWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌无效或已过期") from e


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.models import Customer, User

    payload = decode_token(token)
    role: Role = payload.get("role")
    sub = payload.get("sub")
    if role == "admin":
        result = await db.execute(select(User).where(User.username == sub))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "管理员不存在或已禁用")
        return user
    elif role == "customer":
        result = await db.execute(select(Customer).where(Customer.username == sub))
        customer = result.scalar_one_or_none()
        if not customer:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "客户不存在")
        if customer.status == "pending":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "账号待管理员审批, 请耐心等待")
        if customer.status == "rejected":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被拒绝")
        if not customer.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已禁用")
        return customer
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "角色无效")


async def require_admin(current=Depends(get_current_user)):
    if getattr(current, "role", None) != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return current


async def require_customer(current=Depends(get_current_user)):
    # S16修复: 严格限制 customer 角色,防止 admin ID 混淆写入客户表
    if getattr(current, "role", None) != "customer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要客户权限")
    return current


def _fernet() -> Fernet:
    return Fernet(settings.fernet_key.encode())


class DecryptError(Exception):
    """密钥解密失败异常(P0-2修复: 非 HTTP 上下文不应抛 HTTPException)。"""
    pass


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise DecryptError(f"密钥解密失败: {e}") from e