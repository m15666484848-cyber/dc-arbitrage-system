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
        # 手动迁移:新增字段(IF NOT EXISTS),后续应逐步迁移到 Alembic
        await _migrate_schema(conn)
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


async def _migrate_schema(conn) -> None:
    """手动添加新字段到已有表(PostgreSQL IF NOT EXISTS)。

    Base.metadata.create_all 不会改已有表结构,需要 ALTER TABLE。
    新增字段暂走手动迁移,后续应逐步迁移到 Alembic。
    """
    from sqlalchemy import text

    migrations = [
        # 影子解析对比:只记录新旧解析差异,不参与真实下单链路。
        "CREATE TABLE IF NOT EXISTS parser_shadow_results ("
        "id SERIAL PRIMARY KEY, "
        "signal_id INTEGER REFERENCES signals(id) ON DELETE SET NULL, "
        "kol_id INTEGER REFERENCES kols(id) ON DELETE SET NULL, "
        "discord_message_id VARCHAR(64) DEFAULT '', "
        "raw_text TEXT DEFAULT '', "
        "image_url VARCHAR(512) DEFAULT '', "
        "source VARCHAR(32) DEFAULT 'live', "
        "parse_version VARCHAR(64) DEFAULT '', "
        "old_parsed JSONB DEFAULT '{}'::jsonb, "
        "new_parsed JSONB DEFAULT '{}'::jsonb, "
        "diff JSONB DEFAULT '{}'::jsonb, "
        "mismatch_fields JSONB DEFAULT '[]'::jsonb, "
        "old_status VARCHAR(32) DEFAULT '', "
        "new_status VARCHAR(32) DEFAULT '', "
        "old_symbol VARCHAR(64) DEFAULT '', "
        "new_symbol VARCHAR(64) DEFAULT '', "
        "old_side VARCHAR(16) DEFAULT '', "
        "new_side VARCHAR(16) DEFAULT '', "
        "old_entry_price FLOAT, "
        "new_entry_price FLOAT, "
        "old_stop_loss FLOAT, "
        "new_stop_loss FLOAT, "
        "status VARCHAR(32) DEFAULT 'pending', "
        "review_note TEXT DEFAULT '', "
        "reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL, "
        "reviewed_at TIMESTAMP WITH TIME ZONE, "
        "signal_received_at TIMESTAMP WITH TIME ZONE, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_signal_id ON parser_shadow_results(signal_id)",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_kol_id ON parser_shadow_results(kol_id)",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_status ON parser_shadow_results(status)",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_created_at ON parser_shadow_results(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_signal_received_at ON parser_shadow_results(signal_received_at)",
        "CREATE INDEX IF NOT EXISTS ix_parser_shadow_results_parse_version ON parser_shadow_results(parse_version)",
        # 解析回归测试用例:管理页维护,只用于离线解析验证,不触发下单。
        "CREATE TABLE IF NOT EXISTS parser_regression_cases ("
        "id SERIAL PRIMARY KEY, "
        "name VARCHAR(128) DEFAULT '', "
        "raw_text TEXT DEFAULT '', "
        "image_url VARCHAR(512) DEFAULT '', "
        "expected JSONB DEFAULT '{}'::jsonb, "
        "enabled BOOLEAN DEFAULT TRUE, "
        "tags VARCHAR(256) DEFAULT '', "
        "note TEXT DEFAULT '', "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_parser_regression_cases_name ON parser_regression_cases(name)",
        "CREATE INDEX IF NOT EXISTS ix_parser_regression_cases_enabled ON parser_regression_cases(enabled)",
        "CREATE INDEX IF NOT EXISTS ix_parser_regression_cases_tags ON parser_regression_cases(tags)",
        # 回归测试文件导入报告:按文件/批次保存整体诊断结果,便于回看和按批次清理。
        "CREATE TABLE IF NOT EXISTS parser_regression_import_reports ("
        "id SERIAL PRIMARY KEY, "
        "import_batch_id VARCHAR(64) NOT NULL UNIQUE, "
        "source_file VARCHAR(256) DEFAULT '', "
        "total_messages INTEGER DEFAULT 0, "
        "created_cases INTEGER DEFAULT 0, "
        "high_risk INTEGER DEFAULT 0, "
        "medium_risk INTEGER DEFAULT 0, "
        "low_risk INTEGER DEFAULT 0, "
        "report JSONB DEFAULT '{}'::jsonb, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS ix_parser_regression_import_reports_batch ON parser_regression_import_reports(import_batch_id)",
        "CREATE INDEX IF NOT EXISTS ix_parser_regression_import_reports_created_at ON parser_regression_import_reports(created_at)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS single_exchange_multi_api_limit INTEGER NOT NULL DEFAULT 2",
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
        # kol_follows:用户手动恢复 KOL 后的冷却重置时间
        "ALTER TABLE kol_follows ADD COLUMN IF NOT EXISTS cooldown_reset_at TIMESTAMP WITH TIME ZONE",
        # customers:防共用控制(多开授权 + 单笔下单上限)
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS multi_exchange_allowed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS max_order_usdt FLOAT DEFAULT 5000.0",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS emergency_stop BOOLEAN DEFAULT FALSE",
        # exchange_accounts:API Key 哈希(跨客户唯一性校验,防止多人共用同一 API Key)
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64) DEFAULT ''",
        # exchange_accounts:交易环境。live=实盘,testnet=交易所测试网,demo=Bybit Demo Trading。
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS account_mode VARCHAR(16) DEFAULT 'live'",
        # 部分唯一索引:同一 api_key_hash 只能有一条 is_active=TRUE 的记录(防并发竞态)
        # 使用 IF NOT EXISTS 避免重复创建;CONcurrently 避免锁表(但 ALTER 内不能用,故直接 CREATE)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exchange_accounts_api_key_hash_active "
        "ON exchange_accounts(api_key_hash) "
        "WHERE is_active = TRUE AND api_key_hash != ''",
        # positions:开仓手续费(用于平仓时计算净盈亏,含开仓+平仓手续费)
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_fee FLOAT DEFAULT 0.0",
        # 多 API 跟单:每个 API 独立开关/倍率/策略,订单/持仓/成交/待触发单绑定具体 API
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS follow_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS follow_weight FLOAT DEFAULT 1.0",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS max_order_usdt FLOAT DEFAULT 0.0",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS strategy_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_exchange_accounts_follow_enabled ON exchange_accounts(follow_enabled)",
        "CREATE INDEX IF NOT EXISTS ix_exchange_accounts_strategy_id ON exchange_accounts(strategy_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE equity_snapshots ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_exchange_account_id ON orders(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_positions_exchange_account_id ON positions(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_trades_exchange_account_id ON trades(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_pending_orders_exchange_account_id ON pending_orders(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_equity_snapshots_exchange_account_id ON equity_snapshots(exchange_account_id)",
        # 马丁策略状态:按 KOL + BTC/ETH 隔离存储,避免一个币种亏损影响其它币种。
        "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS martingale_state JSONB DEFAULT '{}'::jsonb",
        # customers:客户分类与邀请系统
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS customer_type VARCHAR(16) DEFAULT 'normal'",
        # customers:客户页面权限,默认隐藏信号汇总
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS show_signal_summary BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS invite_code VARCHAR(16) UNIQUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS invited_by INTEGER REFERENCES customers(id)",
        # 为已有客户生成邀请码
        "UPDATE customers SET invite_code = UPPER(SUBSTRING(MD5(RANDOM()::TEXT || username), 1, 8)) WHERE invite_code IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_customers_customer_type ON customers(customer_type)",
        "CREATE INDEX IF NOT EXISTS idx_customers_invited_by ON customers(invited_by)",
        # Discord 多账号监听:账号表 + KOL 绑定字段
        "CREATE TABLE IF NOT EXISTS discord_accounts ("
        "id SERIAL PRIMARY KEY, "
        "label VARCHAR(64) NOT NULL DEFAULT '默认 Discord 账号', "
        "token_enc TEXT NOT NULL, "
        "token_hash VARCHAR(64) NOT NULL DEFAULT '', "
        "enabled BOOLEAN NOT NULL DEFAULT TRUE, "
        "is_default BOOLEAN NOT NULL DEFAULT FALSE, "
        "last_error TEXT NOT NULL DEFAULT '', "
        "last_connected_at TIMESTAMP WITH TIME ZONE, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS last_error TEXT DEFAULT ''",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS last_connected_at TIMESTAMP WITH TIME ZONE",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_enabled ON discord_accounts(enabled)",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_is_default ON discord_accounts(is_default)",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_token_hash ON discord_accounts(token_hash)",
        "ALTER TABLE kols ADD COLUMN IF NOT EXISTS discord_account_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_kols_discord_account_id ON kols(discord_account_id)",
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_kols_discord_account_id_discord_accounts') THEN "
        "ALTER TABLE kols ADD CONSTRAINT fk_kols_discord_account_id_discord_accounts "
        "FOREIGN KEY(discord_account_id) REFERENCES discord_accounts(id) ON DELETE SET NULL; "
        "END IF; "
        "END $$",
        # 信号去重: discord_message_id 唯一约束,防止 Discord 重连重放导致重复信号
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_signals_discord_message_id_unique') THEN "
        "IF NOT EXISTS (SELECT 1 FROM (SELECT discord_message_id FROM signals GROUP BY discord_message_id HAVING COUNT(*) > 1) dup) THEN "
        "CREATE UNIQUE INDEX idx_signals_discord_message_id_unique ON signals(discord_message_id); "
        "END IF; "
        "END IF; "
        "END $$",
    ]
    applied = 0
    skipped = 0
    for sql in migrations:
        try:
            # M9修复: 使用savepoint,单条迁移失败不影响后续迁移
            async with conn.begin_nested():
                await conn.execute(text(sql))
            applied += 1
        except Exception as e:
            skipped += 1
            logger.debug(f"迁移跳过(可能已存在): {sql[:50]}... -> {e}")
    logger.info(f"数据库迁移完成: {applied} 条执行, {skipped} 条跳过")

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
        # kols:把旧 llm_image_analysis 复制到 vision_llm_enabled (仅当旧列存在时执行)
        col_exists = await conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'kols' AND column_name = 'llm_image_analysis'"
        ))
        if col_exists.scalar():
            await conn.execute(text(
                "UPDATE kols SET vision_llm_enabled = llm_image_analysis "
                "WHERE vision_llm_enabled = FALSE AND llm_image_analysis = TRUE"
            ))
        # 多 API 语义修正:每个客户只保留 1 个默认下单 API。
        # 优先保留:原默认账号 > 无验证错误账号 > 最近验证成功账号 > 最早账号。
        await conn.execute(text(
            "UPDATE exchange_accounts SET account_mode = "
            "CASE "
            "  WHEN testnet = TRUE THEN 'testnet' "
            "  ELSE 'live' "
            "END "
            "WHERE account_mode IS NULL OR account_mode = '' OR account_mode = 'live'"
        ))
        await conn.execute(text(
            "WITH ranked AS ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "    PARTITION BY customer_id "
            "    ORDER BY is_default DESC, (last_error = '') DESC, "
            "             last_verified_at DESC NULLS LAST, id ASC"
            "  ) AS rn "
            "  FROM exchange_accounts "
            "  WHERE is_active = TRUE"
            ") "
            "UPDATE exchange_accounts ea "
            "SET is_default = (ranked.rn = 1) "
            "FROM ranked "
            "WHERE ea.id = ranked.id"
        ))
        # 默认 API 自动参与跟单,保持升级前行为；历史订单/仓位回填到对应客户/交易所/环境的默认账号。
        await conn.execute(text(
            "UPDATE exchange_accounts SET follow_enabled = TRUE "
            "WHERE is_active = TRUE AND is_default = TRUE"
        ))
        await conn.execute(text(
            "WITH chosen AS ("
            "  SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet "
            "  FROM exchange_accounts WHERE is_active = TRUE "
            "  ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, "
            "           last_verified_at DESC NULLS LAST, id ASC"
            ") "
            "UPDATE positions p SET exchange_account_id = c.id "
            "FROM chosen c "
            "WHERE p.exchange_account_id IS NULL "
            "  AND p.customer_id = c.customer_id AND p.exchange = c.exchange"
        ))
        await conn.execute(text(
            "UPDATE orders o SET exchange_account_id = p.exchange_account_id "
            "FROM positions p "
            "WHERE o.exchange_account_id IS NULL AND o.position_id = p.id AND p.exchange_account_id IS NOT NULL"
        ))
        await conn.execute(text(
            "WITH chosen AS ("
            "  SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet "
            "  FROM exchange_accounts WHERE is_active = TRUE "
            "  ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, "
            "           last_verified_at DESC NULLS LAST, id ASC"
            ") "
            "UPDATE orders o SET exchange_account_id = c.id "
            "FROM chosen c "
            "WHERE o.exchange_account_id IS NULL "
            "  AND o.customer_id = c.customer_id AND o.exchange = c.exchange"
        ))
        await conn.execute(text(
            "UPDATE trades t SET exchange_account_id = p.exchange_account_id "
            "FROM positions p "
            "WHERE t.exchange_account_id IS NULL AND t.position_id = p.id AND p.exchange_account_id IS NOT NULL"
        ))
        await conn.execute(text(
            "WITH chosen AS ("
            "  SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet "
            "  FROM exchange_accounts WHERE is_active = TRUE "
            "  ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, "
            "           last_verified_at DESC NULLS LAST, id ASC"
            ") "
            "UPDATE pending_orders po SET exchange_account_id = c.id "
            "FROM chosen c "
            "WHERE po.exchange_account_id IS NULL "
            "  AND po.customer_id = c.customer_id AND po.exchange = c.exchange"
        ))
        await conn.execute(text(
            "WITH chosen AS ("
            "  SELECT DISTINCT ON (customer_id, exchange) id, customer_id, exchange "
            "  FROM exchange_accounts WHERE is_active = TRUE AND last_error = '' "
            "  ORDER BY customer_id, exchange, is_default DESC, "
            "           last_verified_at DESC NULLS LAST, id ASC"
            ") "
            "UPDATE equity_snapshots es SET exchange_account_id = c.id "
            "FROM chosen c "
            "WHERE es.exchange_account_id IS NULL "
            "  AND es.customer_id = c.customer_id AND es.exchange = c.exchange"
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
                except Exception as e:
                    logger.warning(f"Unexpected error: {e}", exc_info=True)
        except Exception as e:
            logger.debug("api_key_hash 回填跳过")
    except Exception as e:
        logger.debug(f"数据迁移跳过(可能字段不存在): {e}")


app = FastAPI(
    title=cfg.app_name,
    description="Discord KOL 实时跟单量化系统",
    version="1.0.0",
    lifespan=lifespan,
)

# === API 限流中间件 ===
RATE_LIMIT_WINDOW = 60  # 60秒窗口
RATE_LIMIT_MAX = 300    # 每窗口最大请求数(看板多端点轮询需余量)
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
    # S7修复: 与 auth.py 保持一致,优先从 x-forwarded-for 提取真实客户端IP
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip() or "unknown"
    else:
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

