"""分析路由:KOL 排行、账户走势、仪表盘、信号汇总。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.models.kol import Kol, KolFollow
from app.models.strategy import Strategy
from app.models.trading import Position
from app.models.config import ExchangeAccount
from app.models.signal import Signal
from app.schemas.common import ok
from app.services import analytics, exchange_adapter, position_manager

def _account_display_name(acc: ExchangeAccount | None) -> str:
    if not acc:
        return ""
    label = (acc.label or "").strip()
    mode = getattr(acc, "account_mode", "") or ("testnet" if acc.testnet else "live")
    return f"{label or f'{acc.exchange.upper()} {mode}'} #{acc.id}"


async def _exchange_account_map(db: AsyncSession, ids: set[int | None]) -> dict[int, ExchangeAccount]:
    account_ids = {int(i) for i in ids if i}
    if not account_ids:
        return {}
    rows = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id.in_(account_ids)))).scalars().all()
    return {r.id: r for r in rows}


router = APIRouter(tags=["分析"])


async def _build_follow_status(db: AsyncSession, customer_id: int, follow: KolFollow) -> dict:
    """构建仪表盘订阅 KOL 状态。"""
    now = datetime.now(timezone.utc)
    paused_until = follow.paused_until
    cooldown_reset_at = getattr(follow, "cooldown_reset_at", None)
    is_paused = bool(paused_until and paused_until > now)

    cooldown_since = now - timedelta(hours=1)
    if cooldown_reset_at and cooldown_reset_at > cooldown_since:
        cooldown_since = cooldown_reset_at

    recent_pos = (
        await db.execute(
            select(Position)
            .where(
                Position.customer_id == customer_id,
                Position.kol_id == follow.kol_id,
                Position.opened_at >= cooldown_since,
            )
            .order_by(Position.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    cooldown_until = (recent_pos.opened_at + timedelta(hours=1)) if recent_pos else None
    is_cooldown = bool(cooldown_until and cooldown_until > now)

    if is_paused:
        status = "paused"
        label = "已暂停"
    elif is_cooldown:
        status = "cooldown"
        label = "冷却中"
    else:
        status = "active"
        label = "正常"

    return {
        "status": status,
        "label": label,
        "paused_until": paused_until,
        "cooldown_until": cooldown_until,
        "cooldown_symbol": getattr(recent_pos, "symbol", "") if recent_pos else "",
        "cooldown_side": getattr(recent_pos, "side", "") if recent_pos else "",
        "can_resume": status in ("paused", "cooldown"),
    }


async def _build_follow_statuses(
    db: AsyncSession,
    customer_id: int,
    follows: list[KolFollow],
) -> dict[int, dict]:
    """批量构建仪表盘订阅 KOL 状态,避免每个 KOL 单独查询 Position。"""
    now = datetime.now(timezone.utc)
    if not follows:
        return {}

    cooldown_since_by_kol: dict[int, datetime] = {}
    for follow in follows:
        cooldown_since = now - timedelta(hours=1)
        cooldown_reset_at = getattr(follow, "cooldown_reset_at", None)
        if cooldown_reset_at and cooldown_reset_at > cooldown_since:
            cooldown_since = cooldown_reset_at
        cooldown_since_by_kol[follow.kol_id] = cooldown_since

    min_cooldown_since = min(cooldown_since_by_kol.values())
    kol_ids = list(cooldown_since_by_kol.keys())
    rows = (
        await db.execute(
            select(Position)
            .where(
                Position.customer_id == customer_id,
                Position.kol_id.in_(kol_ids),
                Position.opened_at >= min_cooldown_since,
            )
            .order_by(Position.kol_id, Position.opened_at.desc())
        )
    ).scalars().all()

    recent_by_kol: dict[int, Position] = {}
    for pos in rows:
        cooldown_since = cooldown_since_by_kol.get(pos.kol_id)
        if not cooldown_since or pos.opened_at < cooldown_since:
            continue
        if pos.kol_id not in recent_by_kol:
            recent_by_kol[pos.kol_id] = pos

    statuses: dict[int, dict] = {}
    for follow in follows:
        paused_until = follow.paused_until
        is_paused = bool(paused_until and paused_until > now)
        recent_pos = recent_by_kol.get(follow.kol_id)
        cooldown_until = (recent_pos.opened_at + timedelta(hours=1)) if recent_pos else None
        is_cooldown = bool(cooldown_until and cooldown_until > now)

        if is_paused:
            status = "paused"
            label = "已暂停"
        elif is_cooldown:
            status = "cooldown"
            label = "冷却中"
        else:
            status = "active"
            label = "正常"

        statuses[follow.kol_id] = {
            "status": status,
            "label": label,
            "paused_until": paused_until,
            "cooldown_until": cooldown_until,
            "cooldown_symbol": getattr(recent_pos, "symbol", "") if recent_pos else "",
            "cooldown_side": getattr(recent_pos, "side", "") if recent_pos else "",
            "can_resume": status in ("paused", "cooldown"),
        }
    return statuses

@router.get("/dashboard")
async def dashboard(
    current=Depends(get_current_user),
    exchange_account_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = current.id if current.role == "customer" else None
    if cid is None:
        return ok({"open_positions": 0, "today_pnl": 0, "total_pnl": 0, "total_trades": 0, "win_rate": 0,
                    "followed_kols": [], "open_positions_list": []})
    stats = await analytics.dashboard_stats(db, cid, exchange_account_id)
    curve = await analytics.equity_curve(db, cid, exchange_account_id=exchange_account_id)

    # 当前订阅的 KOL 列表
    followed = (
        await db.execute(
            select(KolFollow, Kol)
            .join(Kol, KolFollow.kol_id == Kol.id)
            .where(KolFollow.customer_id == cid)
            .order_by(Kol.name)
        )
    ).all()
    followed_kols = []
    # 预加载策略(用于解析跟单金额默认值)
    strategy_ids = {f.strategy_id for f, _ in followed if f.strategy_id}
    strategy_map: dict[int, float] = {}
    if strategy_ids:
        strats = (await db.execute(select(Strategy).where(Strategy.id.in_(strategy_ids)))).scalars().all()
        for s in strats:
            strategy_map[s.id] = (s.params or {}).get("base_qty", 100.0)
    follow_status_map = await _build_follow_statuses(db, cid, [f for f, _ in followed])
    for f, k in followed:
        follow_status = follow_status_map.get(f.kol_id) or await _build_follow_status(db, cid, f)
        if not f.enabled and follow_status["status"] != "paused":
            continue
        # 解析跟单金额: 自定义 > 策略 base_qty > 系统默认 100
        resolved_notional = f.followed_notional_usdt
        if not resolved_notional:
            resolved_notional = strategy_map.get(f.strategy_id, 100.0) if f.strategy_id else 100.0
        followed_kols.append({
            "kol_id": k.id,
            "kol_name": k.name,
            "avatar": k.avatar or "",
            "strategy_id": f.strategy_id,
            "notional_usdt": resolved_notional,
            "notional_source": "custom" if f.followed_notional_usdt else ("strategy" if f.strategy_id else "default"),
            "enabled": f.enabled,
            "paused_until": f.paused_until,
            "cooldown_reset_at": getattr(f, "cooldown_reset_at", None),
            "follow_status": follow_status,
        })

    # Dashboard open position list uses child positions only, same as /positions.
    open_pos_rows = (
        await db.execute(
            select(Position).where(
                Position.customer_id == cid,
                Position.status == "open",
                Position.parent_id.is_not(None),
                Position.exchange_account_id == exchange_account_id if exchange_account_id else True,
            ).order_by(Position.opened_at.desc())
        )
    ).scalars().all()

    # 获取 KOL 名称映射
    kol_ids = {p.kol_id for p in open_pos_rows if p.kol_id}
    kol_map = {}
    if kol_ids:
        kol_map = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()}

    # 获取实时价格
    open_positions_list = []
    price_cache: dict[tuple[str, str], float] = {}
    if open_pos_rows:
        exchange_symbols: dict[str, set[str]] = {}
    account_map = await _exchange_account_map(db, {p.exchange_account_id for p in open_pos_rows})
    for p in open_pos_rows:
        price = price_cache.get((p.exchange, p.symbol), 0.0)
        enriched = await position_manager.enrich_position(p, price, kol_map.get(p.kol_id, ""))
        acc = account_map.get(p.exchange_account_id)
        enriched["exchange_account_label"] = (acc.label or "") if acc else ""
        enriched["exchange_account_name"] = _account_display_name(acc)
        enriched["exchange_account_mode"] = getattr(acc, "account_mode", "") if acc else ""
        open_positions_list.append(enriched)

    return ok({**stats, "equity_curve": curve, "followed_kols": followed_kols,
               "open_positions_list": open_positions_list})


@router.get("/kol-ranking")
async def kol_ranking(days: int = Query(30), db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    return ok(await analytics.kol_ranking(db, days))


@router.get("/equity-curve")
async def equity_curve(
    days: int = Query(30),
    exchange: str | None = Query(None),
    exchange_account_id: int | None = Query(None),
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role != "customer":
        return ok([])
    return ok(await analytics.equity_curve(db, current.id, exchange, days, exchange_account_id))


@router.get("/signals")
async def list_signals(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    kol_id: int | None = Query(None),
    status: str | None = Query(None),
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Signal, Kol.name).join(Kol, Signal.kol_id == Kol.id, isouter=True)
    
    # 客户只能查看自己关注的 KOL 的信号
    if current.role == "customer":
        if not current.show_signal_summary:
            raise HTTPException(403, "信号汇总未开放")
        followed_kols = (await db.execute(
            select(KolFollow.kol_id).where(
                KolFollow.customer_id == current.id,
                KolFollow.enabled.is_(True)
            )
        )).scalars().all()
        if not followed_kols:
            return ok({"items": [], "page": page, "page_size": page_size})
        stmt = stmt.where(Signal.kol_id.in_(followed_kols))
    
    if kol_id:
        stmt = stmt.where(Signal.kol_id == kol_id)
    if status:
        stmt = stmt.where(Signal.status == status)
    stmt = stmt.order_by(Signal.received_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).all()
    items = []
    for sig, kol_name in rows:
        items.append({
            "id": sig.id, "kol_id": sig.kol_id, "kol_name": kol_name or "",
            "raw_text": sig.raw_text[:300], "image_url": sig.image_url,
            "parsed": sig.parsed, "status": sig.status, "dedup_hash": sig.dedup_hash,
            "corrected": sig.corrected, "correct_log": sig.correct_log,
            "confidence": sig.confidence, "symbol": sig.symbol, "side": sig.side,
            "entry_price": sig.entry_price, "note": sig.note,
            "received_at": sig.received_at.isoformat() if sig.received_at else None,
        })
    return ok({"items": items, "page": page, "page_size": page_size})


@router.post("/signals/inject")
async def inject_signal(
    body: dict,
    current=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """手动注入信号(测试用:模拟 KOL 消息)。仅管理员可用,防止客户伪造 KOL 信号触发跟单。"""
    from datetime import datetime, timezone

    from app.services import signal_parser
    from app.services.discord_monitor import _handle_message

    payload = {
        "d": {
            "id": f"inject-{datetime.now(timezone.utc).timestamp()}",
            "channel_id": "",
            "content": body.get("raw_text", ""),
            "author": {"id": "inject"},
            "attachments": [{"content_type": "image/png", "url": body.get("image_url", "")}] if body.get("image_url") else [],
            "embeds": [],
        }
    }
    kol_id = body.get("kol_id")
    if kol_id:
        from app.models.kol import Kol

        kol = (await db.execute(select(Kol).where(Kol.id == kol_id))).scalar_one_or_none()
        if kol:
            payload["d"]["channel_id"] = kol.discord_channel_id
            if kol.discord_user_id:
                payload["d"]["author"]["id"] = kol.discord_user_id
    await _handle_message(payload)
    return ok({"injected": True})
