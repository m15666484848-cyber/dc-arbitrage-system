"""分析路由:KOL 排行、账户走势、仪表盘、信号汇总。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.kol import Kol
from app.models.signal import Signal
from app.schemas.common import ok
from app.services import analytics

router = APIRouter(tags=["分析"])


@router.get("/dashboard")
async def dashboard(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    if cid is None:
        return ok({"open_positions": 0, "today_pnl": 0, "total_pnl": 0, "total_trades": 0, "win_rate": 0})
    stats = await analytics.dashboard_stats(db, cid)
    curve = await analytics.equity_curve(db, cid)
    return ok({**stats, "equity_curve": curve})


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
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动注入信号(测试用:模拟 KOL 消息)。"""
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
