"""管理端路由(仅管理员):管理员账号、客户、时间授权、KOL 管理。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import hash_password, require_admin
from app.models.audit import AuditLog
from app.models.customer import Authorization, Customer
from app.models.kol import Kol
from app.models.user import User
from app.schemas.auth import (
    AuthorizationCreate,
    AuthorizationOut,
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    UserCreate,
    UserOut,
)
from app.schemas.common import ok
from app.schemas.config import SystemConfigUpdate
from app.schemas.kol import KolCreate, KolOut, KolUpdate
from app.services.authz import get_authorization_status

router = APIRouter(prefix="/admin", tags=["管理端"])


async def _audit(db: AsyncSession, user_id: int, action: str, target: str, detail: str = "") -> None:
    db.add(AuditLog(user_id=user_id, action=action, target=target, detail=detail))
    await db.commit()


def _validate_api_key(key: str, label: str) -> None:
    """校验 API Key 格式,拒绝明显的 URL 或其他非法值。"""
    if not key or not key.strip():
        return
    import re
    key = key.strip()
    if re.match(r'^https?://', key, re.IGNORECASE):
        raise HTTPException(400, f"{label} 不能以 http:// 或 https:// 开头,请确认这是 API Key 而非 URL")
    if len(key) < 8:
        raise HTTPException(400, f"{label} 长度过短({len(key)}字符),请检查是否正确")


# ---------- 管理员账号 ----------
@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return ok([UserOut.model_validate(u).model_dump() for u in users])


@router.post("/users")
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    await db.commit()
    await _audit(db, admin.id, "create_user", body.username)
    return ok(UserOut.model_validate(user).model_dump())


# ---------- 客户 ----------
@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    customers = (await db.execute(select(Customer).order_by(Customer.id))).scalars().all()
    out = []
    for c in customers:
        d = CustomerOut.model_validate(c).model_dump()
        auth = await get_authorization_status(db, c.id)
        d["authorized"] = auth["authorized"]
        d["auth_expires_at"] = auth["expires_at"]
        out.append(d)
    return ok(out)


@router.post("/customers")
async def create_customer(body: CustomerCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    exists = (await db.execute(select(Customer).where(Customer.username == body.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "用户名已存在")
    cust = Customer(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        note=body.note,
    )
    db.add(cust)
    await db.commit()
    await _audit(db, admin.id, "create_customer", body.username)
    return ok(CustomerOut.model_validate(cust).model_dump())


@router.put("/customers/{cid}")
async def update_customer(cid: int, body: CustomerUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    if body.display_name is not None:
        cust.display_name = body.display_name
    if body.password:
        cust.password_hash = hash_password(body.password)
    if body.is_active is not None:
        cust.is_active = body.is_active
    if body.note is not None:
        cust.note = body.note
    # 防共用控制(管理员可改)
    if body.multi_exchange_allowed is not None:
        cust.multi_exchange_allowed = body.multi_exchange_allowed
    if body.max_order_usdt is not None:
        cust.max_order_usdt = body.max_order_usdt
    await db.commit()
    await _audit(db, admin.id, "update_customer", str(cid))
    return ok(CustomerOut.model_validate(cust).model_dump())


# ---------- 时间授权 ----------
@router.get("/authorizations/{cid}")
async def list_authorizations(cid: int, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    auths = (await db.execute(select(Authorization).where(Authorization.customer_id == cid).order_by(Authorization.id))).scalars().all()
    return ok([AuthorizationOut.model_validate(a).model_dump() for a in auths])


@router.post("/authorizations")
async def grant_authorization(body: AuthorizationCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    cust = (await db.execute(select(Customer).where(Customer.id == body.customer_id))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    auth = Authorization(
        customer_id=body.customer_id,
        exchange=body.exchange,
        starts_at=body.starts_at,
        expires_at=body.expires_at,
        active=body.active,
        note=body.note,
    )
    db.add(auth)
    await db.commit()
    await _audit(db, admin.id, "grant_auth", f"customer={body.customer_id} exchange={body.exchange}")
    return ok(AuthorizationOut.model_validate(auth).model_dump())


@router.put("/authorizations/{aid}")
async def update_authorization(aid: int, body: AuthorizationCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    auth = (await db.execute(select(Authorization).where(Authorization.id == aid))).scalar_one_or_none()
    if not auth:
        raise HTTPException(404, "授权不存在")
    auth.exchange = body.exchange
    auth.starts_at = body.starts_at
    auth.expires_at = body.expires_at
    auth.active = body.active
    auth.note = body.note
    await db.commit()
    await _audit(db, admin.id, "update_auth", str(aid))
    return ok(AuthorizationOut.model_validate(auth).model_dump())


@router.delete("/authorizations/{aid}")
async def revoke_authorization(aid: int, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    auth = (await db.execute(select(Authorization).where(Authorization.id == aid))).scalar_one_or_none()
    if not auth:
        raise HTTPException(404, "授权不存在")
    auth.active = False
    await db.commit()
    await _audit(db, admin.id, "revoke_auth", str(aid))
    return ok({"id": aid, "active": False})


# ---------- KOL 管理 ----------
@router.get("/kols")
async def list_kols(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
    enabled: bool | None = Query(default=None, description="按启用状态过滤,不传则返回全部"),
):
    query = select(Kol)
    if enabled is not None:
        query = query.where(Kol.enabled == enabled)
    kols = (await db.execute(query.order_by(Kol.id))).scalars().all()
    return ok([KolOut.model_validate(k).model_dump() for k in kols])


@router.post("/kols")
async def create_kol(body: KolCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    kol = Kol(
        name=body.name,
        discord_channel_id=body.discord_channel_id,
        discord_user_id=body.discord_user_id,
        enabled=body.enabled,
        avatar=body.avatar,
        description=body.description,
        llm_enabled=body.llm_enabled,
        vision_llm_enabled=body.vision_llm_enabled,
        llm_fallback=body.llm_fallback,
        llm_min_confidence=body.llm_min_confidence,
    )
    db.add(kol)
    await db.commit()
    await _audit(db, admin.id, "create_kol", body.name)
    return ok(KolOut.model_validate(kol).model_dump())


@router.put("/kols/{kid}")
async def update_kol(kid: int, body: KolUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    kol = (await db.execute(select(Kol).where(Kol.id == kid))).scalar_one_or_none()
    if not kol:
        raise HTTPException(404, "KOL不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(kol, k, v)
    await db.commit()
    await _audit(db, admin.id, "update_kol", str(kid))
    return ok(KolOut.model_validate(kol).model_dump())


@router.delete("/kols/{kid}")
async def delete_kol(kid: int, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    kol = (await db.execute(select(Kol).where(Kol.id == kid))).scalar_one_or_none()
    if not kol:
        raise HTTPException(404, "KOL不存在")
    kol.enabled = False
    await db.commit()
    await _audit(db, admin.id, "delete_kol", str(kid))
    return ok({"id": kid, "enabled": False})


# ---------- 系统配置(LLM + Discord) ----------
def _mask_secret(s: str) -> str:
    """脱敏:仅显示前后 4 位。"""
    if not s:
        return ""
    if len(s) <= 12:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


@router.get("/system-config")
async def get_system_config(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    """读取系统配置(脱敏)。"""
    from app.core.security import decrypt_secret
    from app.models.config import SystemConfig
    from app.schemas.config import SystemConfigOut

    # 直接用请求的 db 查询(不用 ensure_system_config_row,避免跨 session)
    cfg = (await db.execute(select(SystemConfig).where(SystemConfig.id == 1))).scalar_one_or_none()
    if not cfg:
        # 首次访问,创建默认行
        cfg = SystemConfig(id=1)
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)

    text_key = ""
    if cfg.text_llm_api_key_enc:
        try:
            text_key = decrypt_secret(cfg.text_llm_api_key_enc)
        except Exception:
            pass
    vision_key = ""
    if cfg.vision_llm_api_key_enc:
        try:
            vision_key = decrypt_secret(cfg.vision_llm_api_key_enc)
        except Exception:
            pass
    discord_token = ""
    if cfg.discord_token_enc:
        try:
            discord_token = decrypt_secret(cfg.discord_token_enc)
        except Exception:
            pass

    out = SystemConfigOut(
        llm_enabled=cfg.llm_enabled,
        # 文本 LLM
        text_llm_provider=cfg.text_llm_provider,
        text_llm_api_key_mask=_mask_secret(text_key),
        text_llm_api_key_set=bool(text_key),
        text_llm_model=cfg.text_llm_model,
        text_llm_api_base=cfg.text_llm_api_base,
        text_llm_temperature=cfg.text_llm_temperature,
        text_llm_max_tokens=cfg.text_llm_max_tokens,
        text_llm_timeout=cfg.text_llm_timeout,
        # 图片 LLM
        vision_llm_enabled=cfg.vision_llm_enabled,
        vision_llm_provider=cfg.vision_llm_provider,
        vision_llm_api_key_mask=_mask_secret(vision_key),
        vision_llm_api_key_set=bool(vision_key),
        vision_llm_model=cfg.vision_llm_model,
        vision_llm_api_base=cfg.vision_llm_api_base,
        vision_llm_temperature=cfg.vision_llm_temperature,
        vision_llm_max_tokens=cfg.vision_llm_max_tokens,
        vision_llm_timeout=cfg.vision_llm_timeout,
        # Discord
        discord_token_mask=_mask_secret(discord_token),
        discord_token_set=bool(discord_token),
        discord_heartbeat_interval=cfg.discord_heartbeat_interval,
    )
    return ok(out.model_dump())


@router.put("/system-config")
async def update_system_config(
    body: SystemConfigUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)
):
    """更新系统配置。

    api_key / token 字段:
      - None:不修改(保留原值)
      - "":清空
      - 其他:新值,加密存储
    """
    from app.core.security import encrypt_secret
    from app.core.runtime_config import invalidate_cache
    from app.models.config import SystemConfig

    # 用请求的 db 查询(确保 commit 生效)
    cfg = (await db.execute(select(SystemConfig).where(SystemConfig.id == 1))).scalar_one_or_none()
    if not cfg:
        cfg = SystemConfig(id=1)
        db.add(cfg)

    # 全局开关
    if body.llm_enabled is not None:
        cfg.llm_enabled = body.llm_enabled

    # 文本 LLM 普通字段
    if body.text_llm_provider is not None:
        cfg.text_llm_provider = body.text_llm_provider
    if body.text_llm_model is not None:
        cfg.text_llm_model = body.text_llm_model
    if body.text_llm_api_base is not None:
        cfg.text_llm_api_base = body.text_llm_api_base
    if body.text_llm_temperature is not None:
        cfg.text_llm_temperature = body.text_llm_temperature
    if body.text_llm_max_tokens is not None:
        cfg.text_llm_max_tokens = body.text_llm_max_tokens
    if body.text_llm_timeout is not None:
        cfg.text_llm_timeout = body.text_llm_timeout

    # 图片 LLM 普通字段
    if body.vision_llm_enabled is not None:
        cfg.vision_llm_enabled = body.vision_llm_enabled
    if body.vision_llm_provider is not None:
        cfg.vision_llm_provider = body.vision_llm_provider
    if body.vision_llm_model is not None:
        cfg.vision_llm_model = body.vision_llm_model
    if body.vision_llm_api_base is not None:
        cfg.vision_llm_api_base = body.vision_llm_api_base
    if body.vision_llm_temperature is not None:
        cfg.vision_llm_temperature = body.vision_llm_temperature
    if body.vision_llm_max_tokens is not None:
        cfg.vision_llm_max_tokens = body.vision_llm_max_tokens
    if body.vision_llm_timeout is not None:
        cfg.vision_llm_timeout = body.vision_llm_timeout

    # Discord
    if body.discord_heartbeat_interval is not None:
        cfg.discord_heartbeat_interval = body.discord_heartbeat_interval

    # 敏感字段:None=不改,""=清空,其他=新值
    if body.text_llm_api_key is not None:
        _validate_api_key(body.text_llm_api_key, "文本 LLM API Key")
        cfg.text_llm_api_key_enc = encrypt_secret(body.text_llm_api_key) if body.text_llm_api_key else ""
    if body.vision_llm_api_key is not None:
        _validate_api_key(body.vision_llm_api_key, "图片 LLM API Key")
        cfg.vision_llm_api_key_enc = encrypt_secret(body.vision_llm_api_key) if body.vision_llm_api_key else ""
    if body.discord_token is not None:
        cfg.discord_token_enc = encrypt_secret(body.discord_token) if body.discord_token else ""

    await db.commit()
    await db.refresh(cfg)
    invalidate_cache()  # 失效缓存,让下次读取拿到新值
    await _audit(db, admin.id, "update_system_config",
                 f"global_llm={cfg.llm_enabled} "
                 f"text={cfg.text_llm_provider}/key={bool(cfg.text_llm_api_key_enc)} "
                 f"vision={cfg.vision_llm_enabled}/{cfg.vision_llm_provider}/key={bool(cfg.vision_llm_api_key_enc)} "
                 f"discord_set={bool(cfg.discord_token_enc)}")
    return ok({"updated": True})


@router.post("/system-config/test-llm")
async def test_llm_connection(
    llm_type: str = "text",
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """测试 LLM 连接(用当前数据库配置)。

    Args:
        llm_type: "text" 测试文本 LLM, "vision" 测试图片 LLM
    """
    import time
    from app.core.runtime_config import get_text_llm_settings, get_vision_llm_settings
    from app.schemas.config import LLMTestResult

    t0 = time.time()
    try:
        if llm_type == "vision":
            cfg = await get_vision_llm_settings()
            label = "图片 LLM"
        else:
            cfg = await get_text_llm_settings()
            label = "文本 LLM"

        if not cfg.enabled:
            return ok(LLMTestResult(success=False, message=f"{label} 未启用").model_dump())
        if not cfg.api_key:
            return ok(LLMTestResult(success=False, message=f"{label} 未配置 API Key").model_dump())

        # 构造客户端并发一个简单请求
        from app.services.llm_client import LLMClient
        client = LLMClient(
            provider=cfg.provider, api_key=cfg.api_key, model=cfg.model,
            api_base=cfg.api_base, temperature=0, max_tokens=50, timeout=cfg.timeout,
            enabled=True,
        )
        resp = await client.chat([{"role": "user", "content": "ping"}])
        latency = int((time.time() - t0) * 1000)
        return ok(LLMTestResult(
            success=True,
            message=f"{label} 连接成功 - 模型: {cfg.model}, 提供商: {cfg.provider}",
            latency_ms=latency,
            tokens_used=resp.total_tokens,
        ).model_dump())
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        err_str = str(e)
        if "401" in err_str:
            msg = f"连接失败: API Key 无效或未授权(401)。请检查 API Key 是否正确,提供商是否匹配"
        elif "404" in err_str:
            msg = f"连接失败: API 地址错误(404)。请检查模型名称和 API Base URL"
        elif "429" in err_str:
            msg = f"连接失败: 请求过于频繁(429),请稍后重试"
        elif "timeout" in err_str.lower() or "connect" in err_str.lower():
            msg = f"连接失败: 网络超时,请检查 API Base URL 是否可达"
        else:
            msg = f"连接失败: {type(e).__name__}: {e}"
        return ok(LLMTestResult(
            success=False,
            message=msg,
            latency_ms=latency,
        ).model_dump())


# ============ 品种分类倍率 ============

from pydantic import BaseModel


class SymbolNotionalCreate(BaseModel):
    name: str
    symbols: str
    multiplier: float = 1.0
    enabled: bool = True
    note: str = ""


class SymbolNotionalUpdate(BaseModel):
    name: str | None = None
    symbols: str | None = None
    multiplier: float | None = None
    enabled: bool | None = None
    note: str | None = None


@router.get("/symbol-notional")
async def list_symbol_notional_configs(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    from app.models.symbol_config import SymbolNotionalConfig
    rows = (await db.execute(select(SymbolNotionalConfig).order_by(SymbolNotionalConfig.id))).scalars().all()
    return ok([
        {
            "id": r.id,
            "name": r.name,
            "symbols": r.symbols,
            "multiplier": r.multiplier,
            "enabled": r.enabled,
            "note": r.note,
        }
        for r in rows
    ])


@router.post("/symbol-notional")
async def create_symbol_notional_config(
    body: SymbolNotionalCreate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)
):
    from app.models.symbol_config import SymbolNotionalConfig
    if body.multiplier <= 0:
        raise HTTPException(400, "倍率必须大于 0")
    existing = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"分类名 '{body.name}' 已存在")
    cfg = SymbolNotionalConfig(
        name=body.name,
        symbols=body.symbols.upper().strip(),
        multiplier=body.multiplier,
        enabled=body.enabled,
        note=body.note,
    )
    db.add(cfg)
    await db.commit()
    await _audit(db, admin.id, "create_symbol_notional", body.name)
    return ok({"id": cfg.id, "name": cfg.name})


@router.put("/symbol-notional/{cfg_id}")
async def update_symbol_notional_config(
    cfg_id: int, body: SymbolNotionalUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)
):
    from app.models.symbol_config import SymbolNotionalConfig
    cfg = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.id == cfg_id)
    )).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "分类不存在")
    if body.name is not None:
        cfg.name = body.name
    if body.symbols is not None:
        cfg.symbols = body.symbols.upper().strip()
    if body.multiplier is not None:
        if body.multiplier <= 0:
            raise HTTPException(400, "倍率必须大于 0")
        cfg.multiplier = body.multiplier
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.note is not None:
        cfg.note = body.note
    await db.commit()
    await _audit(db, admin.id, "update_symbol_notional", str(cfg_id))
    return ok({"id": cfg_id})


@router.delete("/symbol-notional/{cfg_id}")
async def delete_symbol_notional_config(
    cfg_id: int, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)
):
    from app.models.symbol_config import SymbolNotionalConfig
    cfg = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.id == cfg_id)
    )).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "分类不存在")
    await db.delete(cfg)
    await db.commit()
    await _audit(db, admin.id, "delete_symbol_notional", str(cfg_id))
    return ok({"id": cfg_id})
