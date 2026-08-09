"""安全模块:密码哈希、JWT、RBAC 依赖、Fernet 加密。"""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

Role = Literal["admin", "customer"]


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------- JWT ----------
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


# ---------- 当前用户依赖 ----------
async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """解析 JWT 并返回对应的用户/客户 ORM 对象。"""
    from app.models import Customer, User  # 延迟导入避免循环

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
        if not customer or not customer.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "客户不存在或已禁用")
        return customer
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "角色无效")


async def require_admin(current=Depends(get_current_user)):
    """仅管理员可访问。"""
    if getattr(current, "role", None) != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return current


async def require_customer(current=Depends(get_current_user)):
    """仅客户可访问(下单类操作)。"""
    if getattr(current, "role", None) != "customer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要客户权限")
    return current


# ---------- Fernet 加密(交易所 API Key) ----------
def _fernet() -> Fernet:
    return Fernet(settings.fernet_key.encode())


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
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "密钥解密失败") from e
