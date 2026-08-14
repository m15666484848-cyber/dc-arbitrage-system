"""认证路由:登录、自助注册、当前用户信息。"""
import asyncio
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.customer import Customer
from app.models.user import User
from app.schemas.auth import CustomerRegister, LoginRequest, TokenResponse
from app.schemas.common import ok
from app.services.authz import get_authorization_status

router = APIRouter(prefix="/auth", tags=["认证"])

# ---------- 速率限制 ----------
# 登录: 5 次/分钟;  注册: 10 次/小时
RATE_LIMIT_MAX = 120  # 每分钟最多 5 次
RATE_LIMIT_WINDOW = 60  # 60 秒窗口
LOGIN_FAILURE_MAX = 5  # 连续失败 5 次后锁定
LOGIN_LOCK_SECONDS = 15 * 60  # 锁定 15 分钟


async def _check_rate_limit(
    request: Request,
    action: str,
    max_count: int = RATE_LIMIT_MAX,
    window_sec: int = RATE_LIMIT_WINDOW,
) -> None:
    """基于 IP 的速率限制 (Redis)。

    Args:
        request: FastAPI 请求对象,用于提取客户端 IP
        action: 动作标识 ("login" / "register"),用于区分限流键
        max_count: 窗口内最大允许次数
        window_sec: 时间窗口(秒)
    """
    client_ip = _client_ip(request)
    key = f"rate_limit:{action}:{client_ip}"
    try:
        redis = await get_redis()
        # S10修复: 使用 pipeline(transaction=True) 保证 incr+expire 原子性
        # 防止进程崩溃导致 key 无过期时间,IP 被永久限流
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, window_sec)
            result = await pipe.execute()
        current = result[0]
        if current > max_count:
            logger.warning(f"速率限制触发: action={action} ip={client_ip} count={current}")
            raise HTTPException(429, "请求过于频繁,请稍后再试")
    except HTTPException:
        raise
    except Exception as e:
        # Redis 不可用时降级:不阻断请求,仅记录日志
        logger.warning(f"速率限制检查失败(Redis不可用),降级放行: {e}")


def _client_ip(request: Request) -> str:
    """提取客户端 IP。"""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _login_failure_key(request: Request, username: str) -> str:
    """登录失败锁定键:按 IP + 用户名隔离,避免单 IP 误伤所有账号。"""
    safe_username = (username or "").strip().lower()[:64]
    return f"login_fail:{_client_ip(request)}:{safe_username}"


async def _check_login_lock(request: Request, username: str) -> None:
    """检查登录失败锁定状态。"""
    try:
        redis = await get_redis()
        key = _login_failure_key(request, username)
        current = await redis.get(key)
        if current is not None and int(current) >= LOGIN_FAILURE_MAX:
            raise HTTPException(423, "登录失败次数过多,请15分钟后再试")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"登录失败锁定检查失败(Redis不可用),降级放行: {e}")


async def _record_login_failure(request: Request, username: str) -> None:
    """记录一次登录失败。"""
    try:
        redis = await get_redis()
        key = _login_failure_key(request, username)
        # S10修复: 使用 pipeline 保证 incr+expire 原子性
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, LOGIN_LOCK_SECONDS)
            result = await pipe.execute()
        current = result[0]
        if current >= LOGIN_FAILURE_MAX:
            logger.warning(f"登录失败锁定触发: ip={_client_ip(request)} username={username}")
    except Exception as e:
        logger.warning(f"记录登录失败次数失败(Redis不可用),降级放行: {e}")


async def _clear_login_failures(request: Request, username: str) -> None:
    """登录成功后清理失败计数。"""
    try:
        redis = await get_redis()
        await redis.delete(_login_failure_key(request, username))
    except Exception as e:
        logger.debug(f"清理登录失败计数失败: {e}")


# ---------- 输入校验 ----------
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_username(username: str) -> None:
    """用户名校验:长度 3-20,仅允许字母数字下划线。"""
    if not username:
        raise HTTPException(400, "用户名不能为空")
    if len(username) < 3:
        raise HTTPException(400, "用户名至少 3 位")
    if len(username) > 20:
        raise HTTPException(400, "用户名最多 20 位")
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "用户名仅允许字母、数字和下划线")


def _validate_password(password: str) -> None:
    """密码强度校验:至少 8 位,且包含数字和字母。"""
    if not password:
        raise HTTPException(400, "密码不能为空")
    if len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if not re.search(r"[a-zA-Z]", password):
        raise HTTPException(400, "密码必须包含至少一个字母")
    if not re.search(r"\d", password):
        raise HTTPException(400, "密码必须包含至少一个数字")


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/register")
async def register(body: CustomerRegister, request: Request, db: AsyncSession = Depends(get_db)):
    """客户自助注册 (默认 pending 状态, 需管理员审批后才能登录)。"""
    # 速率限制: 注册 10 次/小时
    await _check_rate_limit(request, "register", max_count=5, window_sec=60)

    # 用户名校验
    _validate_username(body.username)
    # 密码强度校验
    _validate_password(body.password)

    exists = (
        await db.execute(select(Customer).where(Customer.username == body.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "用户名已存在")

    # 邀请码处理:有效则关联邀请人,无效则忽略(正常注册)
    invited_by = None
    register_source = "self"
    if body.invite_code:
        inviter = (
            await db.execute(
                select(Customer).where(Customer.invite_code == body.invite_code.strip())
            )
        ).scalar_one_or_none()
        if inviter:
            invited_by = inviter.id
            register_source = "invite"
            logger.info(
                f"注册邀请码匹配成功: invite_code={body.invite_code} "
                f"inviter_id={inviter.id} inviter_name={inviter.username} "
                f"new_user={body.username}"
            )
        else:
            logger.warning(
                f"注册邀请码无效,忽略并正常注册: invite_code={body.invite_code} "
                f"new_user={body.username}"
            )

    cust = Customer(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
        status="pending",
        is_active=False,
        register_source=register_source,
        invited_by=invited_by,
    )
    db.add(cust)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("注册用户失败")
        raise HTTPException(500, "注册失败,请稍后重试")
    return ok(
        {
            "id": cust.id,
            "username": cust.username,
            "status": "pending",
            "message": "注册成功, 等待管理员审批后即可使用",
        }
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # 速率限制: 登录 5 次/分钟
    await _check_rate_limit(request, "login", max_count=5, window_sec=60)  # S19压测: 临时提高
    await _check_login_lock(request, body.username)

    # S16v2: 并行查询 User 和 Customer,消除时序泄露
    # S19压测修复: asyncio.gather 在同一 session 上并发执行会触发 IllegalStateChangeError
    # 改为顺序查询,性能影响可忽略(两次简单主键查询 < 2ms)
    user_result = await db.execute(select(User).where(User.username == body.username))
    user = user_result.scalar_one_or_none()
    cust_result = await db.execute(select(Customer).where(Customer.username == body.username))
    cust = cust_result.scalar_one_or_none()
    role = "admin"
    if not user and not cust:
        await _record_login_failure(request, body.username)
        raise HTTPException(401, "用户名或密码错误")

    # 客户登录
    if cust:
        if not verify_password(body.password, cust.password_hash):
            await _record_login_failure(request, body.username)
            raise HTTPException(401, "用户名或密码错误")

        if cust.status == "pending":
            raise HTTPException(403, "账号待管理员审批, 请耐心等待")
        if cust.status == "rejected":
            reason = f" (原因: {cust.reject_reason})" if cust.reject_reason else ""
            raise HTTPException(403, f"账号已被拒绝{reason}")
        if not cust.is_active:
            raise HTTPException(403, "账号已禁用")

        cust.last_login_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("更新客户登录时间失败")
            raise HTTPException(500, "登录失败,请稍后重试")
        auth_status = await get_authorization_status(db, cust.id)
        token = create_access_token(cust.username, "customer", {"customer_id": cust.id})
        refresh = create_refresh_token(cust.username, "customer", {"customer_id": cust.id})
        response.set_cookie(
            key="refresh_token",
            value=refresh,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=settings.refresh_token_expire_days * 86400,
            path="/api/auth",
        )
        await _clear_login_failures(request, body.username)
        return TokenResponse(
            access_token=token,
            role="customer",
            user_id=cust.id,
            username=cust.username,
            display_name=cust.display_name,
            authorization=auth_status,
            show_signal_summary=cust.show_signal_summary,
            emergency_stop=cust.emergency_stop,
        )

    # 管理员登录
    if user:
        if not verify_password(body.password, user.password_hash):
            await _record_login_failure(request, body.username)
            raise HTTPException(401, "用户名或密码错误")
        if not user.is_active:
            raise HTTPException(403, "账号已禁用")
        user.last_login_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("更新管理员登录时间失败")
            raise HTTPException(500, "登录失败,请稍后重试")
        token = create_access_token(user.username, "admin", {"user_id": user.id})
        refresh = create_refresh_token(user.username, "admin", {"user_id": user.id})
        response.set_cookie(
            key="refresh_token",
            value=refresh,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=settings.refresh_token_expire_days * 86400,
            path="/api/auth",
        )
        await _clear_login_failures(request, body.username)
        return TokenResponse(
            access_token=token,
            role="admin",
            user_id=user.id,
            username=user.username,
            display_name=user.username,
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
        data["status"] = current.status
        data["authorization"] = auth_status
        data["show_signal_summary"] = current.show_signal_summary
        data["emergency_stop"] = current.emergency_stop
        # 邀请码与邀请链接
        invite_code = current.invite_code or ""
        data["invite_code"] = invite_code
        data["invite_link"] = f"/register?code={invite_code}" if invite_code else ""
        # 邀请人信息
        if current.invited_by:
            inviter = (
                await db.execute(select(Customer).where(Customer.id == current.invited_by))
            ).scalar_one_or_none()
            data["inviter_name"] = inviter.username if inviter else ""
        else:
            data["inviter_name"] = ""
    return ok(data)




@router.post("/refresh")
async def refresh_token(request: Request, db: AsyncSession = Depends(get_db)):
    """使用HttpOnly Cookie中的refresh_token换取新的access_token。"""
    refresh = request.cookies.get("refresh_token")
    if not refresh:
        raise HTTPException(401, "无刷新令牌,请重新登录")

    payload = decode_refresh_token(refresh)
    role = payload.get("role")
    sub = payload.get("sub")

    extra = {}
    if role == "customer":
        extra["customer_id"] = payload.get("customer_id")
    elif role == "admin":
        extra["user_id"] = payload.get("user_id")

    access = create_access_token(sub, role, extra)

    if role == "customer":
        cust = (await db.execute(select(Customer).where(Customer.username == sub))).scalar_one_or_none()
        if not cust or not cust.is_active:
            raise HTTPException(401, "用户不存在或已禁用")
        # S16修复: 密码修改后旧的 refresh_token 失效
        token_iat = payload.get("iat", 0)
        if hasattr(cust, 'password_changed_at') and cust.password_changed_at:
            from datetime import datetime as _dt
            pwd_changed_ts = cust.password_changed_at.timestamp() if hasattr(cust.password_changed_at, 'timestamp') else 0
            if token_iat and token_iat < pwd_changed_ts:
                raise HTTPException(401, "密码已修改,请重新登录")
        auth_status = await get_authorization_status(db, cust.id)
        return ok({
            "access_token": access,
            "role": "customer",
            "user_id": cust.id,
            "username": cust.username,
            "display_name": cust.display_name,
            "authorization": auth_status,
            "show_signal_summary": cust.show_signal_summary,
            "emergency_stop": cust.emergency_stop,
        })
    elif role == "admin":
        user = (await db.execute(select(User).where(User.username == sub))).scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(401, "用户不存在或已禁用")
        # S16修复: 密码修改后旧的 refresh_token 失效
        token_iat = payload.get("iat", 0)
        if hasattr(user, 'password_changed_at') and user.password_changed_at:
            pwd_changed_ts = user.password_changed_at.timestamp() if hasattr(user.password_changed_at, 'timestamp') else 0
            if token_iat and token_iat < pwd_changed_ts:
                raise HTTPException(401, "密码已修改,请重新登录")
        return ok({
            "access_token": access,
            "role": "admin",
            "user_id": user.id,
            "username": user.username,
            "display_name": user.username,
        })
    raise HTTPException(401, "无效的角色")


@router.post("/logout")
async def logout(response: Response):
    """清除refresh_token Cookie。"""
    response.delete_cookie(key="refresh_token", path="/api/auth")
    return ok({"message": "已退出登录"})


@router.get("/my-invitees")
async def my_invitees(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """客户查看自己邀请的用户列表。"""
    if current.role != "customer":
        raise HTTPException(403, "仅客户可查看邀请列表")
    invitees = (
        await db.execute(
            select(Customer)
            .where(Customer.invited_by == current.id)
            .order_by(Customer.created_at.desc())
        )
    ).scalars().all()
    out = []
    for inv in invitees:
        out.append({
            "id": inv.id,
            "username": inv.username,
            "display_name": inv.display_name,
            "status": inv.status,
            "is_active": inv.is_active,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return ok(out)


@router.put("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改当前登录用户密码(管理员或客户均可)。"""
    old_password = body.old_password
    new_password = body.new_password

    if not old_password or not new_password:
        raise HTTPException(400, "请输入旧密码和新密码")
    # 密码强度校验
    _validate_password(new_password)

    if current.role == "admin":
        if not verify_password(old_password, current.password_hash):
            raise HTTPException(400, "旧密码错误")
        current.password_hash = hash_password(new_password)
        current.password_changed_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("修改管理员密码失败")
            raise HTTPException(500, "密码修改失败,请稍后重试")
        return ok({"message": "密码修改成功"})
    elif current.role == "customer":
        if not verify_password(old_password, current.password_hash):
            raise HTTPException(400, "旧密码错误")
        current.password_hash = hash_password(new_password)
        current.password_changed_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("修改客户密码失败")
            raise HTTPException(500, "密码修改失败,请稍后重试")
        return ok({"message": "密码修改成功"})
    else:
        raise HTTPException(403, "未知用户类型")


