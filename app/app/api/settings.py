"""设置路由:交易所账号(加密导入)、风控(静默时段)、飞书告警。"""
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import encrypt_secret, get_current_user, require_customer
from app.models.audit import AlertLog
from app.models.config import AlertConfig, ExchangeAccount, RiskConfig
from app.models.customer import Customer
from app.schemas.common import ok
from app.schemas.config import (
    AlertConfigCreate,
    AlertConfigOut,
    ExchangeAccountCreate,
    ExchangeAccountOut,
    RiskConfigOut,
    RiskConfigUpdate,
)

router = APIRouter(tags=["设置"])


def _mask(key: str) -> str:
    if not key or len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _hash_api_key(api_key: str) -> str:
    """API Key 的 SHA256 哈希,用于跨客户唯一性校验。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# ---------- 交易所账号 ----------
@router.get("/exchange-accounts")
async def list_exchange_accounts(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    if cid is None:
        return ok([])
    rows = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.customer_id == cid).order_by(ExchangeAccount.id))).scalars().all()
    out = []
    for r in rows:
        from app.core.security import decrypt_secret
        d = ExchangeAccountOut.model_validate(r).model_dump()
        try:
            d["api_key_mask"] = _mask(decrypt_secret(r.api_key_enc))
        except Exception:
            d["api_key_mask"] = "***"
        out.append(d)
    return ok(out)


@router.post("/exchange-accounts")
async def add_exchange_account(
    body: ExchangeAccountCreate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    """绑定交易所 API。

    防共用三层校验:
      1. 同客户同交易所已有 API → 拒绝(每客户每交易所只能 1 个 API)
      2. 未授权多开 + 已绑其他交易所 → 拒绝(默认只能绑 1 个交易所)
      3. API Key 哈希已被其他客户绑定 → 拒绝(防止跨客户共用同一 API Key)
    """
    # 取客户信息
    cust = (
        await db.execute(select(Customer).where(Customer.id == current.id))
    ).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")

    # 查该客户已有的活跃 API
    existing = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.customer_id == current.id,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalars().all()

    # 校验 1:同客户同交易所只能 1 个 API
    for acc in existing:
        if acc.exchange == body.exchange:
            raise HTTPException(400, f"该客户已绑定 {body.exchange} 的 API,请先删除旧 API 再绑定新的")

    # 校验 2:未授权多开时,只能绑 1 个交易所
    if existing and not cust.multi_exchange_allowed:
        bound_exchanges = [a.exchange for a in existing]
        raise HTTPException(
            400,
            f"默认每个账号只能绑定 1 个交易所(已绑:{','.join(bound_exchanges)}),多交易所需联系管理员授权",
        )

    # 校验 3:API Key 跨客户唯一(防止多人共用同一 API Key)
    api_key_hash = _hash_api_key(body.api_key)
    dup = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.api_key_hash == api_key_hash,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if dup and dup.customer_id != current.id:
        raise HTTPException(400, "该 API Key 已被其他账号绑定,禁止共用交易所 API")

    acc = ExchangeAccount(
        customer_id=current.id,
        exchange=body.exchange,
        label=body.label,
        api_key_enc=encrypt_secret(body.api_key),
        api_secret_enc=encrypt_secret(body.api_secret),
        passphrase_enc=encrypt_secret(body.passphrase) if body.passphrase else "",
        api_key_hash=api_key_hash,
        testnet=body.testnet,
        is_active=True,
    )
    db.add(acc)
    await db.commit()
    return ok(ExchangeAccountOut.model_validate(acc).model_dump())


@router.delete("/exchange-accounts/{aid}")
async def delete_exchange_account(aid: int, current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    acc = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id == aid, ExchangeAccount.customer_id == current.id))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在")
    acc.is_active = False
    await db.commit()
    return ok({"id": aid, "is_active": False})


@router.post("/exchange-accounts/{aid}/test")
async def test_exchange_account(aid: int, current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    from app.services import exchange_adapter

    acc = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id == aid, ExchangeAccount.customer_id == current.id, ExchangeAccount.is_active.is_(True)))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在或已禁用")

    ex = None
    try:
        ex, _ = await exchange_adapter.load_exchange(db, current.id, acc.exchange, acc.testnet)
        bal = await exchange_adapter.fetch_balance(ex)
        return ok({
            "success": True,
            "exchange": acc.exchange,
            "equity": bal.get("equity", 0),
            "balance": bal.get("balance", 0),
            "message": f"连接成功,权益 {bal.get('equity', 0):.2f} USDT",
        })
    except Exception as e:
        raise HTTPException(400, f"连接失败: {e}")
    finally:
        if ex:
            await exchange_adapter.close_exchange(ex)


# ---------- 风控配置 ----------
@router.get("/risk-config")
async def get_risk_config(current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    cfg = (await db.execute(select(RiskConfig).where(RiskConfig.customer_id == current.id))).scalars().all()
    if not cfg:
        return ok(None)
    return ok([RiskConfigOut.model_validate(c).model_dump() for c in cfg])


@router.put("/risk-config")
async def upsert_risk_config(
    body: RiskConfigUpdate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    existing = (
        await db.execute(select(RiskConfig).where(RiskConfig.customer_id == current.id, RiskConfig.exchange == body.exchange))
    ).scalar_one_or_none()
    if existing:
        for k, v in body.model_dump().items():
            setattr(existing, k, v)
        cfg = existing
    else:
        cfg = RiskConfig(customer_id=current.id, **body.model_dump())
        db.add(cfg)
    await db.commit()
    return ok(RiskConfigOut.model_validate(cfg).model_dump())


# ---------- 飞书告警 ----------
@router.get("/alerts")
async def list_alerts(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    stmt = select(AlertConfig)
    if cid is not None:
        stmt = stmt.where((AlertConfig.customer_id == cid) | (AlertConfig.customer_id.is_(None)))
    rows = (await db.execute(stmt.order_by(AlertConfig.id))).scalars().all()
    return ok([AlertConfigOut.model_validate(r).model_dump() for r in rows])


@router.post("/alerts")
async def add_alert(body: AlertConfigCreate, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # admin 创建全局告警(customer_id=null),customer 创建自己的告警
    cid = None if current.role == "admin" else current.id
    cfg = AlertConfig(customer_id=cid, **body.model_dump())
    db.add(cfg)
    await db.commit()
    return ok(AlertConfigOut.model_validate(cfg).model_dump())


@router.put("/alerts/{aid}")
async def update_alert(aid: int, body: AlertConfigCreate, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # admin 可改任意告警,customer 只能改自己的
    stmt = select(AlertConfig).where(AlertConfig.id == aid)
    if current.role == "customer":
        stmt = stmt.where(AlertConfig.customer_id == current.id)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    await db.commit()
    return ok(AlertConfigOut.model_validate(cfg).model_dump())


@router.delete("/alerts/{aid}")
async def delete_alert(aid: int, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # admin 可删任意告警,customer 只能删自己的
    stmt = select(AlertConfig).where(AlertConfig.id == aid)
    if current.role == "customer":
        stmt = stmt.where(AlertConfig.customer_id == current.id)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")
    cfg.enabled = False
    await db.commit()
    return ok({"id": aid, "enabled": False})


@router.get("/alert-logs")
async def list_alert_logs(
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import desc

    rows = (await db.execute(select(AlertLog).order_by(desc(AlertLog.id)).limit(100))).scalars().all()
    return ok([
        {"id": r.id, "event": r.event, "title": r.title, "success": r.success,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ])
