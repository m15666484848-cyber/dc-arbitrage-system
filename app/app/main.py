"""DC 量化跟单系统 - FastAPI 主应用入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.api import admin, analytics, auth, health, settings, strategy, trading, ws
from app.core.config import settings as cfg
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.logging import setup_logging
from app.core.redis import close_redis
from app.workers.background import start_background_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info(f"启动 {cfg.app_name} (env={cfg.app_env})")

    # 建表(开发期或首次部署;生产用 alembic)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 手动迁移:为已有表添加新字段(IF NOT EXISTS 等效)
        await _migrate_schema(conn)

    # 初始化默认管理员
    await _ensure_admin()

    # 启动后台任务
    await start_background_tasks()

    yield

    logger.info("关闭中...")
    await close_redis()


async def _migrate_schema(conn) -> None:
    """手动添加新字段到已有表(PostgreSQL IF NOT EXISTS)。

    Base.metadata.create_all 不会改已有表结构,需要 ALTER TABLE。
    """
    from sqlalchemy import text

    migrations = [
        # system_config:双 LLM 架构(新增 text_llm_* 和 vision_llm_*)
        # 保留旧 llm_* 字段不删,避免破坏数据
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_provider VARCHAR(32) DEFAULT 'deepseek'",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_api_key_enc TEXT DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_model VARCHAR(64) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_api_base VARCHAR(256) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_temperature FLOAT DEFAULT 0.1",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_max_tokens INTEGER DEFAULT 2000",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_timeout INTEGER DEFAULT 30",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_provider VARCHAR(32) DEFAULT 'zhipu'",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_api_key_enc TEXT DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_model VARCHAR(64) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_api_base VARCHAR(256) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_temperature FLOAT DEFAULT 0.1",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_max_tokens INTEGER DEFAULT 2000",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_timeout INTEGER DEFAULT 60",
        # kols:新增 vision_llm_enabled 字段
        "ALTER TABLE kols ADD COLUMN IF NOT EXISTS vision_llm_enabled BOOLEAN DEFAULT FALSE",
        # customers:防共用控制(多开授权 + 单笔下单上限)
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS multi_exchange_allowed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS max_order_usdt FLOAT DEFAULT 5000.0",
        # exchange_accounts:API Key 哈希(跨客户唯一性校验,防止多人共用同一 API Key)
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64) DEFAULT ''",
        # 部分唯一索引:同一 api_key_hash 只能有一条 is_active=TRUE 的记录(防并发竞态)
        # 使用 IF NOT EXISTS 避免重复创建;CONcurrently 避免锁表(但 ALTER 内不能用,故直接 CREATE)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exchange_accounts_api_key_hash_active "
        "ON exchange_accounts(api_key_hash) "
        "WHERE is_active = TRUE AND api_key_hash != ''",
    ]
    for sql in migrations:
        try:
            await conn.execute(text(sql))
        except Exception as e:
            logger.debug(f"迁移跳过(可能已存在): {sql[:50]}... -> {e}")

    # 数据迁移:首次升级时把旧 llm_* 数据复制到 text_llm_*
    try:
        await conn.execute(text(
            "UPDATE system_config SET text_llm_provider = llm_provider "
            "WHERE text_llm_provider = 'deepseek' AND llm_provider IS NOT NULL AND llm_provider != ''"
        ))
        await conn.execute(text(
            "UPDATE system_config SET text_llm_api_key_enc = llm_api_key_enc "
            "WHERE text_llm_api_key_enc = '' AND llm_api_key_enc IS NOT NULL AND llm_api_key_enc != ''"
        ))
        await conn.execute(text(
            "UPDATE system_config SET text_llm_model = llm_model "
            "WHERE text_llm_model = '' AND llm_model IS NOT NULL AND llm_model != ''"
        ))
        await conn.execute(text(
            "UPDATE system_config SET text_llm_api_base = llm_api_base "
            "WHERE text_llm_api_base = '' AND llm_api_base IS NOT NULL AND llm_api_base != ''"
        ))
        # kols:把旧 llm_image_analysis 复制到 vision_llm_enabled
        await conn.execute(text(
            "UPDATE kols SET vision_llm_enabled = llm_image_analysis "
            "WHERE vision_llm_enabled = FALSE AND llm_image_analysis = TRUE"
        ))
        # exchange_accounts:为已有记录回填 api_key_hash(解密后算 SHA256)
        try:
            from app.core.security import decrypt_secret
            import hashlib

            result = await conn.execute(text(
                "SELECT id, api_key_enc FROM exchange_accounts "
                "WHERE api_key_hash = '' OR api_key_hash IS NULL"
            ))
            for row in result.fetchall():
                try:
                    api_key = decrypt_secret(row[1])
                    h = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
                    await conn.execute(text(
                        "UPDATE exchange_accounts SET api_key_hash = :h WHERE id = :id"
                    ), {"h": h, "id": row[0]})
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"api_key_hash 回填跳过: {e}")
    except Exception as e:
        logger.debug(f"数据迁移跳过(可能字段不存在): {e}")


app = FastAPI(
    title=cfg.app_name,
    description="Discord KOL 实时跟单量化系统",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"未处理异常: {exc}")
    return JSONResponse(
        status_code=500, content={"code": 1, "message": f"服务器错误: {exc}", "data": None}
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


async def _ensure_admin() -> None:
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import User

    async with AsyncSessionLocal() as db:
        exists = (await db.execute(select(User).where(User.username == cfg.admin_username))).scalar_one_or_none()
        if not exists:
            db.add(User(username=cfg.admin_username, password_hash=hash_password(cfg.admin_password)))
            await db.commit()
            logger.info(f"已创建默认管理员: {cfg.admin_username}")
