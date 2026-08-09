"""DC 量化跟单系统 - FastAPI 主应用入口。"""
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Request
from collections import defaultdict, deque
import time as _time
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.api import admin, analytics, auth, health, settings, strategy, trading, ws
from app.core.config import settings as cfg
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.logging import setup_logging
from app.core.redis import close_redis
from app.services.llm_client import close_httpx_client
from app.workers.background import start_background_tasks, stop_background_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(f"启动 {cfg.app_name} (env={cfg.app_env})")

    # 首次部署允许根据当前模型创建基础表;字段演进交给 Alembic。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_schema_migrations()

    # 初始化默认管理员
    await _ensure_admin()

    # 启动后台任务
    await start_background_tasks()

    yield

    logger.info("关闭中...")
    await stop_background_tasks()
    await close_redis()
    # ★ P1 修复: 应用关闭时清理 httpx 共享客户端,释放连接池资源
    await close_httpx_client()


async def _run_schema_migrations() -> None:
    # 运行 Alembic 迁移。
    # 这个项目历史上用 `Base.metadata.create_all()` 初始化基础表,旧 Alembic 版本只覆盖增量字段。
    # 因此首次接管已有库时,如果没有 `alembic_version`,先 stamp 到当前 head;
    # 之后所有字段变更都应新增 Alembic migration,不再把 ALTER TABLE 写在启动入口。
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    async with engine.begin() as conn:
        has_version = bool(await conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")))

    def _upgrade_or_stamp() -> None:
        cfg_path = "alembic.ini"
        alembic_cfg = Config(cfg_path)
        alembic_cfg.set_main_option("script_location", "alembic")

        if has_version:
            command.upgrade(alembic_cfg, "head")
        else:
            # 现网库已由历史启动迁移兜底到当前结构;先建立 Alembic 基线,避免旧增量重复执行。
            command.stamp(alembic_cfg, "head")

    try:
        await asyncio.to_thread(_upgrade_or_stamp)
        logger.info("Alembic 数据库迁移检查完成")
    except Exception as e:
        logger.error(f"Alembic 数据库迁移失败: {e}")
        raise


app = FastAPI(
    title=cfg.app_name,
    description="Discord KOL 实时跟单量化系统",
    version="1.0.0",
    lifespan=lifespan,
)

# === API 限流中间件 ===
RATE_LIMIT_WINDOW = 60  # 60秒窗口
RATE_LIMIT_MAX = 120    # 每窗口最大请求数
RATE_LIMIT_CLEANUP_THRESHOLD = 300  # 清理超过5分钟的条目(秒)
_rate_limit_store: dict[str, deque] = defaultdict(deque)
_last_cleanup = _time.monotonic()


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """简单的滑动窗口限流: 每 IP 每分钟最多 120 次请求。"""
    global _last_cleanup
    # 排除健康检查和 WebSocket 路径,不受限流影响
    path = request.url.path
    if path in ("/api/health", "/health", "/ws") or path.startswith("/ws"):
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    now = _time.monotonic()
    q = _rate_limit_store[client_ip]

    # 清理过期记录
    while q and q[0] < now - RATE_LIMIT_WINDOW:
        q.popleft()

    if len(q) >= RATE_LIMIT_MAX:
        return JSONResponse(
            status_code=429,
            content={"detail": "请求过于频繁,请稍后再试"},
        )

    q.append(now)
    response = await call_next(request)
    # 定期清理空的 deque 条目,防止内存泄漏
    if len(q) == 0 and client_ip in _rate_limit_store:
        del _rate_limit_store[client_ip]

    # P3 修复: 每隔一段时间全局清理超过5分钟未活跃的IP条目,防止内存持续增长
    if now - _last_cleanup > 60:
        _last_cleanup = now
        expired_ips = [
            ip for ip, dq in _rate_limit_store.items()
            if not dq or dq[-1] < now - RATE_LIMIT_CLEANUP_THRESHOLD
        ]
        for ip in expired_ips:
            del _rate_limit_store[ip]
        if expired_ips:
            logger.debug(f"限流存储清理:移除 {len(expired_ips)} 个过期IP条目")

    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With"],
)


# === Security headers middleware ===
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses and hide server version."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' wss: https:; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if "server" in response.headers:
        del response.headers["server"]
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"未处理异常: {exc}")
    if cfg.is_dev:
        msg = f"服务器错误: {exc}"
    else:
        msg = "服务器内部错误,请稍后重试"
    return JSONResponse(
        status_code=500, content={"code": 1, "message": msg, "data": None}
    )


# 路由挂载
api_prefix = "/api"
app.include_router(health.router, prefix=api_prefix)
app.include_router(auth.router, prefix=api_prefix)
app.include_router(admin.router, prefix=api_prefix)
app.include_router(trading.router, prefix=api_prefix)
app.include_router(strategy.router, prefix=api_prefix)
app.include_router(analytics.router, prefix=api_prefix)
app.include_router(settings.router, prefix=api_prefix)
app.include_router(ws.router)  # WebSocket 不加 /api 前缀


@app.get("/health")
async def root_health():
    # 兼容外部监控的根路径健康检查。
    return {"status": "ok"}


async def _ensure_admin() -> None:
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import User

    import os
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")

    async with AsyncSessionLocal() as db:
        exists = (await db.execute(select(User).where(User.username == cfg.admin_username))).scalar_one_or_none()
        if exists:
            # 不再每次启动重置密码,仅确保账号激活
            if not exists.is_active:
                exists.is_active = True
                try:
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    logger.error(f"激活管理员账号失败: {e}")
            logger.info(f"管理员 {cfg.admin_username} 已激活")
        else:
            db.add(User(username=cfg.admin_username, password_hash=hash_password(ADMIN_PASSWORD)))
            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"创建默认管理员失败: {e}")
                raise
            logger.info(f"已创建默认管理员: {cfg.admin_username} (请尽快修改密码)")
