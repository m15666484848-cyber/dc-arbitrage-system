"""管理端路由(仅管理员):管理员账号、客户、时间授权、KOL 管理。"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decrypt_secret, encrypt_secret, hash_password, require_admin, create_access_token
from app.models.audit import AuditLog
from app.models.config import DiscordAccount
from app.models.customer import Authorization, Customer
from app.models.kol import Kol
from app.models.signal import ParserShadowResult, Signal
from app.models.trading import Order
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
from app.schemas.config import DiscordAccountCreate, DiscordAccountOut, DiscordAccountUpdate, SystemConfigUpdate
from app.schemas.kol import KolCreate, KolOut, KolUpdate
from app.services.authz import get_authorization_status
from app.services.discord_monitor import get_source_status

router = APIRouter(prefix="/admin", tags=["管理端"])


async def _audit(db: AsyncSession, user_id: int, action: str, target: str, detail: str = "") -> None:
    db.add(AuditLog(user_id=user_id, action=action, target=target, detail=detail))
    await db.commit()




def _validate_password_strength(password: str) -> None:
    # 统一密码强度:至少 8 位,且包含字母和数字。
    import re

    if not password:
        raise HTTPException(400, "请输入新密码")
    if len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if not re.search(r"[a-zA-Z]", password):
        raise HTTPException(400, "密码必须包含至少一个字母")
    if not re.search(r"\d", password):
        raise HTTPException(400, "密码必须包含至少一个数字")

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


def _hash_discord_token(token: str) -> str:
    """Discord Token 的 SHA256 哈希,用于去重和热重载。"""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _ensure_discord_account_exists(db: AsyncSession, account_id: int | None) -> None:
    """校验 KOL 绑定的 Discord 账号存在且未禁用。"""
    if account_id is None:
        return
    acc = (
        await db.execute(
            select(DiscordAccount).where(
                DiscordAccount.id == account_id,
                DiscordAccount.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(400, "绑定的 Discord 账号不存在或已禁用")


# ---------- Pydantic 输入校验模型 ----------


class SimulateKolSignalRequest(BaseModel):
    message: str
    market_price: float | None = None
    tp_levels: list[float] | None = None
    default_sl_pct: float = -0.05
    no_stop_loss: bool = False
    max_sl_pct: float | None = None


class CustomerAlertCreate(BaseModel):
    name: str = "飞书告警"
    webhook_url: str = ""
    webhook_secret: str = ""
    enabled: bool = True
    on_signal: bool = False
    on_order: bool = True
    on_tp_sl: bool = True
    on_correct: bool = True
    on_risk: bool = True
    on_auth_expire: bool = True
    on_error: bool = True


class CustomerAlertUpdate(BaseModel):
    name: str | None = None
    webhook_url: str | None = None
    webhook_secret: str | None = None
    enabled: bool | None = None
    on_signal: bool | None = None
    on_order: bool | None = None
    on_tp_sl: bool | None = None
    on_correct: bool | None = None
    on_risk: bool | None = None
    on_auth_expire: bool | None = None
    on_error: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class CustomerTypeUpdate(BaseModel):
    customer_type: Literal["normal", "internal"]


class ShadowReviewRequest(BaseModel):
    status: Literal["pending", "accepted", "rejected", "ignored"]
    review_note: str = ""


# ---------- 转发源状态 ----------
@router.get("/source-status")
async def get_forward_source_status(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """查看 Discord 转发源/监听连接状态。"""
    from app.core.runtime_config import get_discord_settings

    cfg = await get_discord_settings()
    enabled_discord_accounts = (
        await db.execute(select(DiscordAccount).where(DiscordAccount.enabled.is_(True)))
    ).scalars().all()
    enabled_kol_count = (
        await db.execute(select(Kol).where(Kol.enabled.is_(True)))
    ).scalars().all()
    last_signal = (
        await db.execute(select(Signal).order_by(Signal.received_at.desc()).limit(1))
    ).scalar_one_or_none()

    status = get_source_status()
    status["configured"] = bool(cfg.token or enabled_discord_accounts)
    status["discord_account_count"] = len(enabled_discord_accounts)
    status["enabled_kol_count"] = len(enabled_kol_count)
    status["last_signal_at"] = last_signal.received_at if last_signal else None
    status["last_signal_kol_id"] = last_signal.kol_id if last_signal else None
    status["last_signal_symbol"] = last_signal.symbol if last_signal else ""
    status["heartbeat_interval"] = cfg.heartbeat_interval
    return ok(status)


# ---------- 影子解析对比 ----------
@router.get("/shadow-results")
async def list_shadow_results(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=50, ge=10, le=200, description="每页数量"),
    hours: int = Query(default=168, ge=1, le=2160, description="回看最近多少小时"),
    kol_id: int | None = Query(default=None, description="KOL ID"),
    status: str | None = Query(default=None, description="审核状态"),
    mismatch_only: bool = Query(default=False, description="只看有差异的结果"),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """查询影子解析结果。只读接口，不影响真实下单。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    base_stmt = select(ParserShadowResult).where(
        ParserShadowResult.created_at >= since
    )
    if kol_id:
        base_stmt = base_stmt.where(ParserShadowResult.kol_id == kol_id)
    if status:
        base_stmt = base_stmt.where(ParserShadowResult.status == status)
    if mismatch_only:
        base_stmt = base_stmt.where(func.jsonb_array_length(ParserShadowResult.mismatch_fields) > 0)

    total = await db.scalar(select(func.count()).select_from(base_stmt.order_by(None).subquery()))
    pending_total = await db.scalar(
        select(func.count()).select_from(
            base_stmt.order_by(None)
            .where(ParserShadowResult.status == "pending")
            .subquery()
        )
    )
    mismatch_total = await db.scalar(
        select(func.count()).select_from(
            base_stmt.order_by(None)
            .where(func.jsonb_array_length(ParserShadowResult.mismatch_fields) > 0)
            .subquery()
        )
    )

    rows = (
        await db.execute(
            base_stmt.order_by(ParserShadowResult.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    kol_ids = {r.kol_id for r in rows if r.kol_id}
    kol_map: dict[int, str] = {}
    if kol_ids:
        kols = (
            await db.execute(select(Kol.id, Kol.name).where(Kol.id.in_(kol_ids)))
        ).all()
        kol_map = {k.id: k.name for k in kols}

    def row_out(r: ParserShadowResult) -> dict:
        return {
            "id": r.id,
            "signal_id": r.signal_id,
            "kol_id": r.kol_id,
            "kol_name": kol_map.get(r.kol_id or 0, ""),
            "discord_message_id": r.discord_message_id,
            "raw_text": r.raw_text,
            "image_url": r.image_url,
            "source": r.source,
            "parse_version": r.parse_version,
            "old_parsed": r.old_parsed or {},
            "new_parsed": r.new_parsed or {},
            "diff": r.diff or {},
            "mismatch_fields": r.mismatch_fields or [],
            "old_status": r.old_status,
            "new_status": r.new_status,
            "old_symbol": r.old_symbol,
            "new_symbol": r.new_symbol,
            "old_side": r.old_side,
            "new_side": r.new_side,
            "old_entry_price": r.old_entry_price,
            "new_entry_price": r.new_entry_price,
            "old_stop_loss": r.old_stop_loss,
            "new_stop_loss": r.new_stop_loss,
            "status": r.status,
            "review_note": r.review_note,
            "reviewer_id": r.reviewer_id,
            "reviewed_at": r.reviewed_at,
            "signal_received_at": r.signal_received_at,
            "created_at": r.created_at,
        }

    return ok({
        "total": total or 0,
        "page": page,
        "page_size": page_size,
        "items": [row_out(r) for r in rows],
        "summary": {
            "pending": pending_total or 0,
            "mismatch": mismatch_total or 0,
            "matched": max((total or 0) - (mismatch_total or 0), 0),
        },
        "filters": {
            "hours": hours,
            "kol_id": kol_id,
            "status": status,
            "mismatch_only": mismatch_only,
        },
    })


@router.post("/shadow-results/{result_id}/review")
async def review_shadow_result(
    result_id: int,
    body: ShadowReviewRequest,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """人工审核影子解析结果。只更新影子表状态。"""
    row = (
        await db.execute(
            select(ParserShadowResult).where(ParserShadowResult.id == result_id)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "影子解析结果不存在")
    row.status = body.status
    row.review_note = body.review_note.strip()
    row.reviewer_id = admin.id
    row.reviewed_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("审核影子解析结果失败")
        raise HTTPException(500, "审核失败,请稍后重试")
    await _audit(db, admin.id, "review_shadow_result", str(result_id), f"status={body.status}")
    return ok({"id": row.id, "status": row.status, "review_note": row.review_note})


# ---------- 跟单诊断 ----------
@router.get("/diagnosis")
async def get_follow_diagnosis(
    hours: int = Query(default=24, ge=1, le=720, description="回看最近多少小时"),
    limit: int = Query(default=100, ge=10, le=500, description="每类最多返回条数"),
    signal_status: str | None = Query(default=None, description="信号状态过滤"),
    order_status: str | None = Query(default=None, description="订单状态过滤"),
    customer_id: int | None = Query(default=None, description="客户ID过滤"),
    kol_id: int | None = Query(default=None, description="KOL ID过滤"),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """聚合信号处理、订单失败和审计日志,用于排查为什么没跟单。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    customers = (await db.execute(select(Customer))).scalars().all()
    customer_map = {c.id: (c.display_name or c.username) for c in customers}
    kols = (await db.execute(select(Kol))).scalars().all()
    kol_map = {k.id: k.name for k in kols}

    order_stmt = select(Order).where(Order.created_at >= since)
    if customer_id:
        order_stmt = order_stmt.where(Order.customer_id == customer_id)
    if kol_id:
        order_stmt = order_stmt.where(Order.kol_id == kol_id)
    if order_status:
        order_stmt = order_stmt.where(Order.status == order_status)
    orders = (
        await db.execute(order_stmt.order_by(Order.created_at.desc()).limit(limit))
    ).scalars().all()

    related_signal_ids = {o.signal_id for o in orders if o.signal_id}
    signal_stmt = select(Signal).where(Signal.received_at >= since)
    if kol_id:
        signal_stmt = signal_stmt.where(Signal.kol_id == kol_id)
    if signal_status:
        signal_stmt = signal_stmt.where(Signal.status == signal_status)
    if customer_id and related_signal_ids:
        signal_stmt = signal_stmt.where(Signal.id.in_(related_signal_ids))
    signals = (
        await db.execute(signal_stmt.order_by(Signal.received_at.desc()).limit(limit))
    ).scalars().all()

    signal_ids = {s.id for s in signals}
    if signal_ids:
        linked_orders = (
            await db.execute(
                select(Order)
                .where(Order.signal_id.in_(signal_ids))
                .order_by(Order.created_at.desc())
                .limit(limit * 5)
            )
        ).scalars().all()
    else:
        linked_orders = []
    orders_by_signal: dict[int, list[Order]] = {}
    for order in linked_orders:
        if order.signal_id:
            orders_by_signal.setdefault(order.signal_id, []).append(order)

    audit_stmt = select(AuditLog).where(AuditLog.created_at >= since)
    audit_logs = (
        await db.execute(audit_stmt.order_by(AuditLog.created_at.desc()).limit(100))
    ).scalars().all()

    def order_out(o: Order) -> dict:
        return {
            "id": o.id,
            "customer_id": o.customer_id,
            "customer_name": customer_map.get(o.customer_id, f"客户{o.customer_id}"),
            "kol_id": o.kol_id,
            "kol_name": kol_map.get(o.kol_id, ""),
            "signal_id": o.signal_id,
            "exchange": o.exchange,
            "symbol": o.symbol,
            "side": o.side,
            "type": o.type,
            "qty": o.qty,
            "price": o.price,
            "leverage": o.leverage,
            "status": o.status,
            "filled_qty": o.filled_qty,
            "filled_price": o.filled_price,
            "error_msg": o.error_msg,
            "created_at": o.created_at,
            "filled_at": o.filled_at,
            "deleted_at": o.deleted_at,
            "tp_level": o.tp_level,
        }

    def signal_reason(s: Signal) -> str:
        if s.note:
            return s.note
        if s.correct_log:
            return s.correct_log
        if s.status in ("rejected", "filtered", "ignored"):
            return "信号未进入下单流程,但未记录更详细原因"
        if s.status == "ordered":
            return "信号已进入下单流程,请查看关联订单状态"
        return "仍在处理链路中或暂无诊断备注"

    signal_items = []
    for s in signals:
        p = s.parsed or {}
        linked = [order_out(o) for o in orders_by_signal.get(s.id, [])]
        signal_items.append({
            "id": s.id,
            "kol_id": s.kol_id,
            "kol_name": kol_map.get(s.kol_id, ""),
            "status": s.status,
            "symbol": s.symbol or p.get("symbol", ""),
            "side": s.side or p.get("side", ""),
            "entry_price": s.entry_price or p.get("entry_price"),
            "confidence": s.confidence,
            "corrected": s.corrected,
            "correct_log": s.correct_log,
            "note": s.note,
            "reason": signal_reason(s),
            "raw_text": s.raw_text,
            "image_url": s.image_url,
            "received_at": s.received_at,
            "orders": linked,
            "order_count": len(linked),
            "failed_order_count": len([o for o in linked if o.get("status") == "failed" or o.get("error_msg")]),
        })

    failed_orders = [
        order_out(o)
        for o in orders
        if o.status == "failed" or bool(o.error_msg) or order_status
    ]

    summary = {
        "signals": len(signal_items),
        "orders": len(orders),
        "failed_orders": len([o for o in orders if o.status == "failed" or bool(o.error_msg)]),
        "rejected_signals": len([s for s in signals if s.status == "rejected"]),
        "filtered_signals": len([s for s in signals if s.status == "filtered"]),
        "ignored_signals": len([s for s in signals if s.status == "ignored"]),
        "ordered_signals": len([s for s in signals if s.status == "ordered"]),
    }

    return ok({
        "summary": summary,
        "signals": signal_items,
        "orders": [order_out(o) for o in orders],
        "failed_orders": failed_orders,
        "audit_logs": [
            {
                "id": a.id,
                "action": a.action,
                "target": a.target,
                "detail": a.detail,
                "ip": a.ip,
                "created_at": a.created_at,
            }
            for a in audit_logs
        ],
        "filters": {
            "hours": hours,
            "limit": limit,
            "signal_status": signal_status,
            "order_status": order_status,
            "customer_id": customer_id,
            "kol_id": kol_id,
        },
    })


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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建用户失败")
        raise HTTPException(500, "创建用户失败,请稍后重试")
    await _audit(db, admin.id, "create_user", body.username)
    return ok(UserOut.model_validate(user).model_dump())


# ---------- 客户 ----------
@router.get("/customers")
async def list_customers(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    customers = (await db.execute(select(Customer).order_by(Customer.id))).scalars().all()
    # 预加载邀请人用户名映射
    inviter_ids = {c.invited_by for c in customers if c.invited_by}
    inviter_map: dict[int, str] = {}
    if inviter_ids:
        inviters = (await db.execute(
            select(Customer.id, Customer.username).where(Customer.id.in_(inviter_ids))
        )).all()
        inviter_map = {i.id: i.username for i in inviters}
    out = []
    for c in customers:
        d = CustomerOut.model_validate(c).model_dump()
        auth = await get_authorization_status(db, c.id)
        d["authorized"] = auth["authorized"]
        d["auth_expires_at"] = auth["expires_at"]
        d["inviter_name"] = inviter_map.get(c.invited_by, "")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建客户失败")
        raise HTTPException(500, "创建客户失败,请稍后重试")
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
    if body.status is not None:
        cust.status = body.status
    if body.is_active is not None:
        cust.is_active = body.is_active
    if body.note is not None:
        cust.note = body.note
    # 防共用控制(管理员可改)
    if body.single_exchange_multi_api_allowed is not None:
        cust.single_exchange_multi_api_allowed = body.single_exchange_multi_api_allowed
    if body.single_exchange_multi_api_limit is not None:
        limit = int(body.single_exchange_multi_api_limit or 1)
        if limit < 1:
            raise HTTPException(400, "单交易所多 API 数量至少为 1")
        if limit > 20:
            raise HTTPException(400, "单交易所多 API 数量不能超过 20")
        cust.single_exchange_multi_api_limit = limit
    if body.multi_exchange_allowed is not None:
        cust.multi_exchange_allowed = body.multi_exchange_allowed
    if body.max_order_usdt is not None:
        cust.max_order_usdt = body.max_order_usdt
    if body.show_signal_summary is not None:
        cust.show_signal_summary = body.show_signal_summary
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新客户失败")
        raise HTTPException(500, "更新客户失败,请稍后重试")
    await _audit(db, admin.id, "update_customer", str(cid))
    return ok(CustomerOut.model_validate(cust).model_dump())


# ---------- 时间授权 ----------
@router.delete("/customers/{cid}")
async def delete_customer(cid: int, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    """删除客户及其所有关联数据(级联删除:授权/策略/持仓/订单/交易/告警等)。"""
    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    cust_name = cust.username
    await db.delete(cust)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除客户失败")
        raise HTTPException(500, "删除客户失败,请稍后重试")
    await _audit(db, admin.id, "delete_customer", f"customer:{cid}", f"删除客户 {cust_name}")
    return ok({"message": f"客户 {cust_name} 已删除"})


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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("授权失败")
        raise HTTPException(500, "授权失败,请稍后重试")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新授权失败")
        raise HTTPException(500, "更新授权失败,请稍后重试")
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
    await _ensure_discord_account_exists(db, body.discord_account_id)
    kol = Kol(
        name=body.name,
        discord_account_id=body.discord_account_id,
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建KOL失败")
        raise HTTPException(500, "创建KOL失败,请稍后重试")
    await _audit(db, admin.id, "create_kol", body.name)
    return ok(KolOut.model_validate(kol).model_dump())


@router.put("/kols/{kid}")
async def update_kol(kid: int, body: KolUpdate, db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    kol = (await db.execute(select(Kol).where(Kol.id == kid))).scalar_one_or_none()
    if not kol:
        raise HTTPException(404, "KOL不存在")
    if "discord_account_id" in body.model_dump(exclude_unset=True):
        await _ensure_discord_account_exists(db, body.discord_account_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(kol, k, v)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新KOL失败")
        raise HTTPException(500, "更新KOL失败,请稍后重试")
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


# ---------- Discord 账号管理 ----------
def _discord_account_out(acc: DiscordAccount) -> dict:
    token = ""
    if acc.token_enc:
        try:
            token = decrypt_secret(acc.token_enc)
        except Exception:
            token = ""
    data = DiscordAccountOut.model_validate(acc).model_dump()
    data["token_mask"] = _mask_secret(token)
    data["token_set"] = bool(token)
    return data


async def _unset_other_default_discord_accounts(db: AsyncSession, account_id: int | None = None) -> None:
    rows = (await db.execute(select(DiscordAccount))).scalars().all()
    for row in rows:
        if account_id is None or row.id != account_id:
            row.is_default = False


@router.get("/discord-accounts")
async def list_discord_accounts(db: AsyncSession = Depends(get_db), admin=Depends(require_admin)):
    rows = (await db.execute(select(DiscordAccount).order_by(DiscordAccount.is_default.desc(), DiscordAccount.id))).scalars().all()
    return ok([_discord_account_out(r) for r in rows])


@router.post("/discord-accounts")
async def create_discord_account(
    body: DiscordAccountCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "Discord Token 不能为空")
    token_hash = _hash_discord_token(token)
    dup = (
        await db.execute(
            select(DiscordAccount).where(
                DiscordAccount.token_hash == token_hash,
                DiscordAccount.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(400, "该 Discord Token 已存在")

    if body.is_default:
        await _unset_other_default_discord_accounts(db)

    acc = DiscordAccount(
        label=body.label,
        token_enc=encrypt_secret(token),
        token_hash=token_hash,
        enabled=body.enabled,
        is_default=body.is_default,
    )
    db.add(acc)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建Discord账号失败")
        raise HTTPException(500, "创建Discord账号失败,请稍后重试")
    await db.refresh(acc)

    from app.core.runtime_config import invalidate_cache

    invalidate_cache()
    await _audit(db, admin.id, "create_discord_account", acc.label)
    return ok(_discord_account_out(acc))


@router.put("/discord-accounts/{account_id}")
async def update_discord_account(
    account_id: int,
    body: DiscordAccountUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    acc = (await db.execute(select(DiscordAccount).where(DiscordAccount.id == account_id))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "Discord 账号不存在")

    if body.label is not None:
        acc.label = body.label
    if body.enabled is not None:
        acc.enabled = body.enabled
    if body.is_default is not None:
        if body.is_default:
            await _unset_other_default_discord_accounts(db, account_id=acc.id)
        acc.is_default = body.is_default
    if body.token is not None:
        token = body.token.strip()
        if not token:
            raise HTTPException(400, "Discord Token 不能为空")
        token_hash = _hash_discord_token(token)
        dup = (
            await db.execute(
                select(DiscordAccount).where(
                    DiscordAccount.token_hash == token_hash,
                    DiscordAccount.id != account_id,
                    DiscordAccount.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if dup:
            raise HTTPException(400, "该 Discord Token 已存在")
        acc.token_enc = encrypt_secret(token)
        acc.token_hash = token_hash
        acc.last_error = ""

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新Discord账号失败")
        raise HTTPException(500, "更新Discord账号失败,请稍后重试")
    await db.refresh(acc)

    from app.core.runtime_config import invalidate_cache

    invalidate_cache()
    await _audit(db, admin.id, "update_discord_account", str(account_id))
    return ok(_discord_account_out(acc))


@router.delete("/discord-accounts/{account_id}")
async def delete_discord_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    acc = (await db.execute(select(DiscordAccount).where(DiscordAccount.id == account_id))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "Discord 账号不存在")
    acc.enabled = False
    acc.is_default = False
    await db.commit()

    from app.core.runtime_config import invalidate_cache

    invalidate_cache()
    await _audit(db, admin.id, "delete_discord_account", str(account_id))
    return ok({"id": account_id, "enabled": False})


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

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新系统配置失败")
        raise HTTPException(500, "更新系统配置失败,请稍后重试")
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
    if llm_type not in ("text", "vision"):
        raise HTTPException(400, "llm_type 只能是 text 或 vision")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建品种分类倍率失败")
        raise HTTPException(500, "创建品种分类倍率失败,请稍后重试")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新品种分类倍率失败")
        raise HTTPException(500, "更新品种分类倍率失败,请稍后重试")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除品种分类倍率失败")
        raise HTTPException(500, "删除品种分类倍率失败,请稍后重试")
    await _audit(db, admin.id, "delete_symbol_notional", str(cfg_id))
    return ok({"id": cfg_id})


# ============ 危险操作:清除测试数据 ============

from pydantic import BaseModel as PydanticBaseModel


class ResetDataConfirm(PydanticBaseModel):
    """清除数据请求体,需输入确认文字防止误操作。"""
    confirm_text: str
    reset_strategy_state: bool = True  # 是否重置策略马丁格尔状态
    customer_id: int | None = None  # 指定客户ID,为None时清除所有数据



@router.post("/simulate-kol-signal")
async def simulate_kol_signal(payload: SimulateKolSignalRequest, _: Customer = Depends(require_admin)):
    """Admin safe simulation for one KOL text signal. No database writes, no orders."""
    from app.services.signal_parser import parse_text, classify_signal_intent, classify_signal_scene
    from app.services.signal_filter import apply_defaults, correct_direction, correct_price
    from app.services.strategy_engine import get_strategy_defaults
    from app.services.order_manager import _build_tp_levels

    raw_message = (payload.message or "").strip()
    if not raw_message:
        raise HTTPException(status_code=400, detail="请输入要模拟的 KOL 消息")

    parsed = parse_text(raw_message)
    parsed_before = parsed.model_dump()
    intent, intent_reason = classify_signal_intent(raw_message)
    scene, scene_reason = classify_signal_scene(raw_message)

    import re

    def _evidence(pattern: str) -> str:
        m = re.search(pattern, raw_message, re.IGNORECASE)
        return m.group(0) if m else ""

    analysis_basis: list[dict] = []

    def add_basis(item: str, rule: str, evidence: str, result: str) -> None:
        analysis_basis.append({
            "item": item,
            "rule": rule,
            "evidence": evidence or "未命中明确片段",
            "result": result,
        })

    add_basis(
        "交易品种",
        "优先识别 $TICKER、交易对、常见英文币种和中文币种映射",
        _evidence(r"(比特币|大饼|BTC\s*/?\s*USDT|BTC)") if parsed.symbol else "",
        parsed.symbol or "未识别",
    )
    add_basis(
        "交易方向",
        "匹配做多/多单/long/buy 等多头词，或做空/空单/short/sell 等空头词",
        _evidence(r"(做多|多单|开多|买入|long|buy|做空|空单|开空|short|sell)") if parsed.side else "",
        parsed.side or "未识别",
    )
    add_basis(
        "入场价格",
        "从现价/入场/进场/开仓等关键词后的价格中提取",
        _evidence(r"(现价|入场|进场|开仓)\s*[:：]?\s*[0-9]+(?:\.[0-9]+)?"),
        str(parsed.entry_price or "未识别"),
    )
    add_basis(
        "止损价格",
        "从止损/SL/防守位等关键词后的价格中提取；多单止损应低于入场，空单止损应高于入场",
        _evidence(r"(止损|SL|sl|防守位|防守)\s*[:：]?\s*[^0-9]{0,8}[0-9]+(?:\.[0-9]+)?"),
        str(parsed.stop_loss or "未识别"),
    )
    add_basis(
        "止盈价格",
        "从止盈/目标/TP 等关键词后的价格中提取；如果命中待定/暂无/无，则视为缺失止盈，后续按策略默认止盈补全",
        _evidence(r"(止盈|目标|TP|tp)\s*[:：]?\s*[^\n，,。；;]*"),
        "原文未给明确止盈" if not parsed.take_profits else str(parsed.take_profits),
    )
    add_basis(
        "意图判断",
        "先排除公告/噪音/分析复盘，再判断是否为交易动作；unknown 会继续走结构化解析",
        raw_message[:120],
        f"{intent}: {intent_reason}",
    )
    add_basis(
        "场景判断",
        "识别分析、条件观察、叙述、噪音等非直接交易场景",
        raw_message[:120],
        f"{scene}: {scene_reason}",
    )

    steps: list[dict] = []
    steps.append({
        "name": "文本解析",
        "status": "ok" if parsed.confidence > 0 else "ignored",
        "message": f"品种={parsed.symbol or '未识别'}, 方向={parsed.side or '未识别'}, 入场={parsed.entry_price}, 止损={parsed.stop_loss}, 止盈={parsed.take_profits}",
    })
    steps.append({
        "name": "意图识别",
        "status": "ok" if intent in ("trade", "unknown") else "reject",
        "message": f"{intent}: {intent_reason}",
    })
    steps.append({
        "name": "场景识别",
        "status": "ok" if scene not in ("analysis", "conditional_observe", "narrative", "noise") else "reject",
        "message": f"{scene}: {scene_reason}",
    })

    decision = "accept"
    reject_reason = ""

    if parsed.is_exit_signal:
        decision = "exit_signal"
        steps.append({"name": "信号类型", "status": "ok", "message": f"平仓信号: {parsed.exit_reason}"})
    elif parsed.is_update_signal:
        decision = "update_signal"
        steps.append({"name": "信号类型", "status": "ok", "message": f"止盈止损更新: {parsed.update_reason}"})
    else:
        if intent == "noise":
            decision = "reject"
            reject_reason = f"噪音/公告: {intent_reason}"
        elif intent == "analysis":
            decision = "reject"
            reject_reason = f"分析/复盘/假设: {intent_reason}"
        elif not parsed.symbol:
            decision = "reject"
            reject_reason = "无交易品种"
        elif not parsed.side:
            decision = "reject"
            reject_reason = "无交易方向"

        if decision != "reject":
            price_log = ""
            price_rejected = False
            try:
                _, price_log, price_rejected = correct_price(parsed, payload.market_price)
            except Exception as e:
                price_log = f"价格纠错异常: {e}"
                price_rejected = True
            if price_log:
                steps.append({"name": "价格纠错", "status": "reject" if price_rejected else "corrected", "message": price_log})
            if price_rejected:
                decision = "reject"
                reject_reason = price_log

        if decision != "reject":
            _, dir_log = correct_direction(parsed)
            if dir_log:
                steps.append({"name": "方向纠错", "status": "corrected", "message": dir_log})

            params = {"tp_levels": payload.tp_levels or [3, 5, 8], "default_sl_pct": payload.default_sl_pct, "no_stop_loss": payload.no_stop_loss}
            defaults = get_strategy_defaults(params)
            default_log = apply_defaults(
                parsed,
                payload.market_price,
                defaults["default_tp_pct"],
                defaults["default_sl_pct"],
                defaults["no_stop_loss"],
                payload.max_sl_pct,
            )
            if default_log:
                steps.append({"name": "缺省补全", "status": "corrected", "message": default_log})
                add_basis(
                    "补全止盈止损",
                    "当原文缺失止盈或止损时，按模拟参数/策略默认值生成；默认止盈来自 tp_levels，默认止损来自 default_sl_pct",
                    str(params),
                    default_log,
                )
            else:
                steps.append({"name": "缺省补全", "status": "ok", "message": "止盈/止损信息完整,无需补全"})
                add_basis(
                    "补全止盈止损",
                    "原文已经提供所需风险参数，或当前信号类型无需补全",
                    str(params),
                    "无需补全",
                )

    ref_entry = parsed.entry_price or payload.market_price
    tp_preview = []
    if parsed.symbol and parsed.side and ref_entry and ref_entry > 0:
        try:
            tp_preview = _build_tp_levels(parsed, {"tp_levels": payload.tp_levels or [3, 5, 8]}, float(ref_entry), parsed.side)
        except Exception as e:
            steps.append({"name": "止盈分级", "status": "warn", "message": f"生成止盈分级失败: {e}"})

    order_preview = None
    if decision not in ("reject", "exit_signal", "update_signal") and parsed.symbol and parsed.side:
        order_preview = {
            "would_open": True,
            "symbol": parsed.symbol,
            "side": parsed.side,
            "order_side": "buy" if parsed.side == "long" else "sell",
            "entry_price": ref_entry,
            "stop_loss": parsed.stop_loss,
            "take_profits": parsed.take_profits,
            "tp_levels": tp_preview,
            "note": "这里只是模拟预览,不会创建信号、订单或持仓。",
        }

    return {
        "decision": decision,
        "reject_reason": reject_reason,
        "input": {
            "message": raw_message,
            "market_price": payload.market_price,
            "tp_levels": payload.tp_levels or [3, 5, 8],
            "default_sl_pct": payload.default_sl_pct,
            "no_stop_loss": payload.no_stop_loss,
            "max_sl_pct": payload.max_sl_pct,
        },
        "intent": {"value": intent, "reason": intent_reason},
        "scene": {"value": scene, "reason": scene_reason},
        "parsed_before": parsed_before,
        "parsed_after": parsed.model_dump(),
        "steps": steps,
        "analysis_basis": analysis_basis,
        "order_preview": order_preview,
        "safe_mode": True,
    }


@router.post("/customers/{cid}/login-as")
async def login_as_customer(cid: int, admin=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """管理员以客户身份登录(生成客户令牌,不记录登录时间)。"""
    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    if cust.status != "active" or not cust.is_active:
        raise HTTPException(400, "客户未激活或未审批")
    auth_status = await get_authorization_status(db, cust.id)
    token = create_access_token(cust.username, "customer", {"customer_id": cust.id})
    await _audit(db, admin.id, "login_as_customer", f"customer:{cust.username}({cust.id})")
    return ok({
        "access_token": token,
        "role": "customer",
        "user_id": cust.id,
        "username": cust.username,
        "display_name": cust.display_name,
        "authorization": auth_status,
        "show_signal_summary": cust.show_signal_summary,
        "emergency_stop": cust.emergency_stop,
    })



@router.post("/reset-data")
async def reset_test_data(
    body: ResetDataConfirm,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """清除测试数据(信号/订单/持仓/交易/日志),保留配置。

    当 customer_id 为 None 时清除所有数据;
    当 customer_id 指定时只清除该客户的数据。

    清除的表(全部模式):
      - signals / orders / positions / trades / pending_orders
      - alert_logs / equity_snapshots
      (audit_logs 不可清除,保留完整审计轨迹)

    清除的表(按客户模式):
      - orders / positions / trades / pending_orders / equity_snapshots
      - alert_logs (通过 alert_configs 关联)
      (signals 和 audit_logs 无 customer_id,按客户模式不清除)

    保留的表(配置):
      - users / customers / authorizations / kols / kol_follows
      - exchange_accounts / risk_configs / alert_configs
      - system_config / symbol_notional_configs
      - customer_symbol_multipliers

    策略表(strategies):
      - 配置保留,可选重置马丁格尔运行状态(martingale_round/last_result/last_qty)
    """
    cid = body.customer_id

    # 验证确认文字
    if cid is None:
        expected_text = "确认清除所有测试数据"
        scope = "所有客户"
    else:
        # 按客户清除时,验证客户存在
        cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
        if not cust:
            raise HTTPException(404, "客户不存在")
        expected_text = f"确认清除 {cust.display_name or cust.username} 的数据"
        scope = f"客户 {cust.display_name or cust.username}(ID:{cid})"

    if body.confirm_text != expected_text:
        raise HTTPException(400, f"确认文字不匹配,请输入: {expected_text}")

    from sqlalchemy import text

    stats = {}

    if cid is None:
        # ===== 全部清除模式 =====
        clear_tables = [
            ("alert_logs", "DELETE FROM alert_logs"),
            ("equity_snapshots", "DELETE FROM equity_snapshots"),
            ("trades", "DELETE FROM trades"),
            ("pending_orders", "DELETE FROM pending_orders"),
            ("orders", "DELETE FROM orders"),
            ("positions", "DELETE FROM positions"),
            ("signals", "DELETE FROM signals"),
            # audit_logs 不可清除,保留完整审计轨迹
        ]
        for name, sql in clear_tables:
            result = await db.execute(text(sql))
            stats[name] = result.rowcount

        if body.reset_strategy_state:
            result = await db.execute(text(
                "UPDATE strategies SET martingale_round = 0, last_result = '', last_qty = 0.0"
            ))
            stats["strategies_reset"] = result.rowcount
    else:
        # ===== 按客户清除模式 =====
        # 有 customer_id 的表直接按客户删除
        cust_tables = [
            ("equity_snapshots", "DELETE FROM equity_snapshots WHERE customer_id = :cid"),
            ("trades", "DELETE FROM trades WHERE customer_id = :cid"),
            ("pending_orders", "DELETE FROM pending_orders WHERE customer_id = :cid"),
            ("orders", "DELETE FROM orders WHERE customer_id = :cid"),
            ("positions", "DELETE FROM positions WHERE customer_id = :cid"),
        ]
        for name, sql in cust_tables:
            result = await db.execute(text(sql), {"cid": cid})
            stats[name] = result.rowcount

        # alert_logs 通过 alert_configs 关联客户
        result = await db.execute(text(
            "DELETE FROM alert_logs WHERE alert_config_id IN "
            "(SELECT id FROM alert_configs WHERE customer_id = :cid)"
        ), {"cid": cid})
        stats["alert_logs"] = result.rowcount

        # 重置该客户策略的马丁格尔状态
        if body.reset_strategy_state:
            result = await db.execute(text(
                "UPDATE strategies SET martingale_round = 0, last_result = '', last_qty = 0.0 "
                "WHERE customer_id = :cid"
            ), {"cid": cid})
            stats["strategies_reset"] = result.rowcount

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("清除测试数据失败")
        raise HTTPException(500, "清除测试数据失败,请稍后重试")

    # 记录审计日志(在清除后重新写入)
    db.add(AuditLog(
        user_id=admin.id,
        action="reset_test_data",
        target=f"customer_id={cid}" if cid else "all_trading_data",
        detail=f"清除范围: {scope}, 统计: {stats}",
    ))
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("记录审计日志失败")
        raise HTTPException(500, "清除数据成功但审计日志写入失败,请联系管理员")

    total = sum(v for k, v in stats.items() if k != "strategies_reset")
    return ok({"cleared": stats, "total_deleted": total, "scope": scope})

# ---------- 客户告警管理 ----------
@router.get("/customers/{cid}/alerts")
async def list_customer_alerts(cid: int, _=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """管理员查看指定客户的告警配置。"""
    from app.models.config import AlertConfig
    stmt = select(AlertConfig).where(AlertConfig.customer_id == cid).order_by(AlertConfig.id)
    rows = (await db.execute(stmt)).scalars().all()
    return ok([
        {
            "id": r.id,
            "customer_id": r.customer_id,
            "name": r.name,
            "webhook_url": r.webhook_url,
            "webhook_secret_set": bool(r.webhook_secret),
            "enabled": r.enabled,
            "on_signal": r.on_signal,
            "on_order": r.on_order,
            "on_tp_sl": r.on_tp_sl,
            "on_correct": r.on_correct,
            "on_risk": r.on_risk,
            "on_auth_expire": r.on_auth_expire,
            "on_error": r.on_error,
        }
        for r in rows
    ])


@router.post("/customers/{cid}/alerts")
async def create_customer_alert(cid: int, body: CustomerAlertCreate, _=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """管理员为客户创建告警配置。"""
    from app.models.config import AlertConfig
    cfg = AlertConfig(
        customer_id=cid,
        name=body.name,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        enabled=body.enabled,
        on_signal=body.on_signal,
        on_order=body.on_order,
        on_tp_sl=body.on_tp_sl,
        on_correct=body.on_correct,
        on_risk=body.on_risk,
        on_auth_expire=body.on_auth_expire,
        on_error=body.on_error,
    )
    db.add(cfg)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"创建失败: {e}")
    return ok({"id": cfg.id, "message": "告警配置已创建"})


@router.put("/customers/{cid}/alerts/{aid}")
async def update_customer_alert(cid: int, aid: int, body: CustomerAlertUpdate, _=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """管理员修改客户告警配置。"""
    from app.models.config import AlertConfig
    stmt = select(AlertConfig).where(AlertConfig.id == aid, AlertConfig.customer_id == cid)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")

    update_data = body.model_dump(exclude_unset=True)
    for field in ["name", "webhook_url", "webhook_secret", "enabled",
                   "on_signal", "on_order", "on_tp_sl", "on_correct",
                   "on_risk", "on_auth_expire", "on_error"]:
        if field in update_data:
            setattr(cfg, field, update_data[field])

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"更新失败: {e}")
    return ok({"message": "告警配置已更新"})


@router.delete("/customers/{cid}/alerts/{aid}")
async def delete_customer_alert(cid: int, aid: int, _=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """管理员删除客户告警配置。"""
    from app.models.config import AlertConfig
    stmt = select(AlertConfig).where(AlertConfig.id == aid, AlertConfig.customer_id == cid)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")

    await db.delete(cfg)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"删除失败: {e}")
    return ok({"message": "告警配置已删除"})


@router.post("/customers/{cid}/reset-password")
async def reset_customer_password(
    cid: int,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """管理员重置客户密码。"""
    new_password = body.new_password
    _validate_password_strength(new_password)

    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")

    cust.password_hash = hash_password(new_password)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("重置客户密码失败")
        raise HTTPException(500, "重置密码失败,请稍后重试")
    await _audit(db, admin.id, "reset_customer_password", str(cid))
    return ok({"message": f"客户 {cust.username} 密码已重置"})


# ============ 利润统计 & 邀请系统 ============
@router.get("/profit-stats")
async def profit_stats(
    start_date: str = Query(..., description="开始日期 (ISO,如 2026-01-01)"),
    end_date: str = Query(..., description="结束日期 (ISO,如 2026-01-31)"),
    customer_type: str | None = Query(
        default=None, description="客户分类过滤: normal(普通) | internal(内部),不传则全部"
    ),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """按时间范围统计每个客户的利润、手续费、邀请佣金。

    返回字段:
      - customer_id, username, display_name, customer_type
      - total_pnl (净盈亏,Trade.realized_pnl 之和,is_close=True)
      - total_fee (手续费,Trade.fee 之和)
      - trade_count (平仓交易数)
      - commission_earned (邀请佣金收入,ReferralCommission.commission_amount 之和)
      - invited_count (邀请人数)
      - inviter_name (邀请人用户名,无则空字符串)
    """
    from app.services.analytics import customer_profit_stats

    start_dt = _parse_iso_date(start_date, "start_date")
    end_dt = _parse_iso_date(end_date, "end_date")
    # 将结束日期扩展到当天 23:59:59.999999,使日期范围包含整天
    end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    if start_dt > end_dt:
        raise HTTPException(400, "start_date 不能晚于 end_date")
    if customer_type and customer_type not in ("normal", "internal"):
        raise HTTPException(400, "customer_type 只能是 normal 或 internal")

    rows = await customer_profit_stats(db, start_dt, end_dt, customer_type)
    return ok(rows)


@router.get("/profit-stats/export")
async def profit_stats_export(
    start_date: str = Query(..., description="开始日期 (ISO,如 2026-01-01)"),
    end_date: str = Query(..., description="结束日期 (ISO,如 2026-01-31)"),
    customer_type: str | None = Query(
        default=None, description="客户分类过滤: normal(普通) | internal(内部),不传则全部"
    ),
    profit_percentage: float | None = Query(
        default=None, description="利润分成百分比(0-100),如 50 表示计算利润的50%。不传或0则不计算分成"
    ),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """导出利润统计为 Excel(xlsx) 文件。

    参数同 /admin/profit-stats,额外支持 profit_percentage 利润分成百分比。
    返回 StreamingResponse,文件名包含日期范围。
    """
    import io

    from app.services.analytics import customer_profit_stats

    start_dt = _parse_iso_date(start_date, "start_date")
    end_dt = _parse_iso_date(end_date, "end_date")
    end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    if start_dt > end_dt:
        raise HTTPException(400, "start_date 不能晚于 end_date")
    if customer_type and customer_type not in ("normal", "internal"):
        raise HTTPException(400, "customer_type 只能是 normal 或 internal")

    pct = float(profit_percentage) if profit_percentage else 0.0

    rows = await customer_profit_stats(db, start_dt, end_dt, customer_type)

    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise HTTPException(500, f"服务端缺少 openpyxl 依赖: {e}")

    wb = Workbook()
    ws = wb.active
    ws.title = "利润统计"

    if pct > 0:
        headers = [
            "客户ID", "用户名", "显示名", "客户类型",
            "净盈亏(USDT)", "手续费(USDT)", "净利润(USDT)", "平仓交易数",
            f"分成金额({pct}%)(USDT)",
            "邀请佣金收入(USDT)", "邀请人数", "邀请人用户名",
        ]
    else:
        headers = [
            "客户ID", "用户名", "显示名", "客户类型",
            "净盈亏(USDT)", "手续费(USDT)", "平仓交易数",
            "邀请佣金收入(USDT)", "邀请人数", "邀请人用户名",
        ]
    ws.append(headers)

    for r in rows:
        pnl = round(float(r.get("total_pnl", 0) or 0), 4)
        fee = round(float(r.get("total_fee", 0) or 0), 4)
        net = round(pnl - fee, 4)
        if pct > 0:
            share = round(pnl * pct / 100.0, 4)
            ws.append([
                r.get("customer_id"),
                r.get("username", ""),
                r.get("display_name", ""),
                r.get("customer_type", ""),
                pnl, fee, net,
                int(r.get("trade_count", 0) or 0),
                share,
                round(float(r.get("commission_earned", 0) or 0), 4),
                int(r.get("invited_count", 0) or 0),
                r.get("inviter_name", "") or "",
            ])
        else:
            ws.append([
                r.get("customer_id"),
                r.get("username", ""),
                r.get("display_name", ""),
                r.get("customer_type", ""),
                pnl, fee,
                int(r.get("trade_count", 0) or 0),
                round(float(r.get("commission_earned", 0) or 0), 4),
                int(r.get("invited_count", 0) or 0),
                r.get("inviter_name", "") or "",
            ])

    # 列宽
    for col_idx, header in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(
            len(str(header)) * 2, 14
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    tag = f"_{pct}pct" if pct > 0 else ""
    filename = f"profit_stats_{start_date}_{end_date}{tag}.xlsx"
    quoted = filename.replace(" ", "_")

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{quoted}"; filename*=UTF-8\'\'{quoted}'
        },
    )


@router.get("/customers/{cid}/invite-link")
async def get_customer_invite_link(
    cid: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """返回客户的邀请码和邀请链接。

    邀请链接格式: /register?code={invite_code}
    """
    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    invite_code = cust.invite_code or ""
    invite_link = f"/register?code={invite_code}" if invite_code else ""
    return ok({"invite_code": invite_code, "invite_link": invite_link})


@router.put("/customers/{cid}/type")
async def update_customer_type(
    cid: int,
    body: CustomerTypeUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
):
    """更新客户分类 (normal 普通客户 | internal 内部用户)。"""
    customer_type = body.customer_type

    cust = (await db.execute(select(Customer).where(Customer.id == cid))).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")

    old_type = cust.customer_type
    cust.customer_type = customer_type
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新客户分类失败")
        raise HTTPException(500, "更新客户分类失败,请稍后重试")
    await _audit(
        db, admin.id, "update_customer_type", str(cid),
        f"{cust.username}: {old_type} -> {customer_type}",
    )
    return ok({
        "id": cust.id,
        "username": cust.username,
        "customer_type": cust.customer_type,
    })


def _parse_iso_date(raw: str, field_name: str) -> datetime:
    """解析 ISO 日期字符串(支持 'YYYY-MM-DD' 或完整 ISO 时间),失败抛 400。

    返回带 UTC 时区的 datetime(日期部分按当天的 00:00:00 UTC)。
    """
    if not raw:
        raise HTTPException(400, f"{field_name} 不能为空")
    # 兼容纯日期 'YYYY-MM-DD' 与完整 ISO 'YYYY-MM-DDTHH:MM:SS'
    try:
        # 先尝试纯日期
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"{field_name} 格式错误,应为 ISO 日期(如 2026-01-01)")
    return dt.replace(tzinfo=timezone.utc)


# ============ 交易所对账 ============
@router.post("/reconcile")
async def trigger_reconciliation(
    _=Depends(require_admin),
):
    """手动触发交易所对账。

    遍历所有活跃客户的交易所账号,比对本地 DB 与交易所实际持仓/挂单:
      - 幽灵持仓(本地有但交易所无) → 自动标记 closed
      - 孤儿持仓(交易所有但本地无) → 告警
      - 数量不一致 → 告警
      - 幽灵挂单(本地有但交易所无) → 自动标记 cancelled
      - 孤儿挂单(交易所有但本地无) → 告警

    返回完整对账报告。
    """
    from app.services.reconciliation import run_reconciliation

    report = await run_reconciliation()
    return ok(report.to_dict())


@router.get("/reconcile/latest")
async def get_latest_reconciliation(
    _=Depends(require_admin),
):
    """获取最近一次对账报告(来自定时任务或手动触发的缓存)。"""
    from app.services.reconciliation import get_last_report

    report = get_last_report()
    if not report:
        return ok({"message": "暂无对账记录", "report": None})
    return ok(report.to_dict())
