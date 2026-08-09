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
from app.models.signal import Signal
from app.schemas.common import ok
from app.services import analytics, exchange_adapter, position_manager

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


@router.get("/dashboard")
async def dashboard(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    if cid is None:
        return ok({"open_positions": 0, "today_pnl": 0, "total_pnl": 0, "total_trades": 0, "win_rate": 0,
                    "followed_kols": [], "open_positions_list": []})
    stats = await analytics.dashboard_stats(db, cid)
    curve = await analytics.equity_curve(db, cid)

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
    for f, k in followed:
        follow_status = await _build_follow_status(db, cid, f)
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

    # 当前持仓列表 (master 仓位)
    open_pos_rows = (
        await db.execute(
            select(Position).where(
                Position.customer_id == cid,
                Position.status == "open",
                Position.parent_id.is_(None),
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
        for p in open_pos_rows:
            exchange_symbols.setdefault(p.exchange, set()).add(p.symbol)
        for exh, syms in exchange_symbols.items():
            try:
                prices = await exchange_adapter.fetch_market_prices_batch(exh, list(syms))
                for sym, price in prices.items():
                    price_cache[(exh, sym)] = price
            except Exception:
                pass

    for p in open_pos_rows:
        price = price_cache.get((p.exchange, p.symbol), 0.0)
        enriched = await position_manager.enrich_position(p, price, kol_map.get(p.kol_id, ""))
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
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role != "customer":
        return ok([])
    return ok(await analytics.equity_curve(db, current.id, exchange, days))


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
