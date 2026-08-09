"""交易路由(客户视图):KOL 跟随、持仓、订单、成交、手动平仓/删除/下单、止损修改。"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_customer
from app.models.kol import Kol, KolFollow
from app.models.trading import Order, Position, Trade
from app.schemas.common import ok
from app.schemas.kol import KolFollowUpdate, KolOut
from app.schemas.trading import (
    ClosePositionRequest,
    DeleteOrderRequest,
    ManualOrderRequest,
    UpdateStopRequest,
)
from app.services import exchange_adapter, order_manager, position_manager

router = APIRouter(tags=["交易"])


def _resolve_customer(current, customer_id: int | None) -> int:
    """客户只能查自己;管理员可指定。"""
    if current.role == "customer":
        return current.id
    if customer_id:
        return customer_id
    raise HTTPException(400, "管理员需指定 customer_id")


# ---------- KOL 跟随 ----------
@router.get("/kols")
async def list_kols_for_customer(
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cid = _resolve_customer(current, None) if current.role == "customer" else None
    kols = (await db.execute(select(Kol).where(Kol.enabled.is_(True)).order_by(Kol.id))).scalars().all()
    followed_ids: set[int] = set()
    follow_map: dict[int, dict] = {}
    if cid:
        rows = (await db.execute(select(KolFollow).where(KolFollow.customer_id == cid, KolFollow.enabled.is_(True)))).scalars().all()
        for f in rows:
            followed_ids.add(f.kol_id)
            follow_map[f.kol_id] = {
                "strategy_id": f.strategy_id,
                "notional_usdt": f.followed_notional_usdt,
            }
    out = []
    for k in kols:
        d = KolOut.model_validate(k).model_dump()
        d["followed"] = k.id in followed_ids
        d["follow_settings"] = follow_map.get(k.id)
        out.append(d)
    return ok(out)


@router.post("/kols/follow")
async def set_follows(
    body: KolFollowUpdate,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """批量设置关注的 KOL(多选/全选),支持每 KOL 独立策略和跟单金额。"""
    cid = current.id

    # 解析: 优先使用 kol_settings(精细模式),否则用 kol_ids(简化模式)
    if body.kol_settings:
        # 精细模式: 每个 KOL 独立设置
        settings_map: dict[int, dict] = {s.kol_id: {"strategy_id": s.strategy_id, "notional_usdt": s.notional_usdt} for s in body.kol_settings}
        target_ids = set(settings_map.keys())
    elif body.kol_ids is not None:
        # 简化模式: 统一设置
        target_ids = set(body.kol_ids)
        settings_map = {kid: {"strategy_id": body.strategy_id, "notional_usdt": body.notional_usdt} for kid in target_ids}
    else:
        raise HTTPException(400, "必须提供 kol_ids 或 kol_settings")

    # 获取现有关注记录
    existing = (await db.execute(select(KolFollow).where(KolFollow.customer_id == cid))).scalars().all()
    existing_map = {f.kol_id: f for f in existing}

    # 更新或创建
    for kol_id, settings in settings_map.items():
        if kol_id in existing_map:
            f = existing_map[kol_id]
            f.enabled = True
            f.strategy_id = settings["strategy_id"]
            f.followed_notional_usdt = settings["notional_usdt"]
        else:
            db.add(KolFollow(
                customer_id=cid,
                kol_id=kol_id,
                strategy_id=settings["strategy_id"],
                followed_notional_usdt=settings["notional_usdt"],
                enabled=True,
            ))

    # 取消不在目标列表中的关注
    for f in existing:
        if f.kol_id not in target_ids:
            f.enabled = False

    await db.commit()
    return ok({"followed": list(target_ids)})


# ---------- 持仓 ----------
@router.get("/positions")
async def list_positions(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = _resolve_customer(current, customer_id)
    # 持仓管理只展示未结束仓位；已平仓仓位应进入历史记录。
    # 仍保留 master + sub 结构，前端 Positions.tsx 按 parent_id 分组展示。
    positions = (
        await db.execute(
            select(Position)
            .where(Position.customer_id == cid, Position.status == "open")
            .order_by(Position.opened_at.desc())
        )
    ).scalars().all()
    kol_ids = {p.kol_id for p in positions if p.kol_id}
    kol_map = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()} if kol_ids else {}

    open_positions = [p for p in positions if p.status == "open"]
    price_cache: dict[tuple[str, str], float] = {}
    if open_positions:
        exchange_symbols: dict[str, set[str]] = {}
        for p in open_positions:
            exchange_symbols.setdefault(p.exchange, set()).add(p.symbol)
        for exh, syms in exchange_symbols.items():
            prices = await exchange_adapter.fetch_market_prices_batch(exh, list(syms))
            for sym, price in prices.items():
                price_cache[(exh, sym)] = price

    out = []
    for p in positions:
        price = price_cache.get((p.exchange, p.symbol), 0.0) if p.status == "open" else 0.0
        out.append(await position_manager.enrich_position(p, price, kol_map.get(p.kol_id, "")))
    return ok(out)


@router.post("/positions/close")
async def close_position_api(
    body: ClosePositionRequest,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    pos = (await db.execute(select(Position).where(Position.id == body.position_id))).scalar_one_or_none()
    if not pos or (current.role == "customer" and pos.customer_id != current.id):
        raise HTTPException(404, "持仓不存在")
    result = await order_manager.close_position(db, body.position_id, body.qty)
    return ok(result)


@router.put("/positions/stop")
async def update_stop(
    body: UpdateStopRequest,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    pos = (await db.execute(select(Position).where(Position.id == body.position_id))).scalar_one_or_none()
    if not pos or (current.role == "customer" and pos.customer_id != current.id):
        raise HTTPException(404, "持仓不存在")
    if body.sl is not None:
        pos.sl = body.sl
    if body.trailing_stop is not None:
        pos.trailing_stop = body.trailing_stop
    if body.trailing_callback is not None:
        pos.trailing_callback = body.trailing_callback
    await db.commit()
    return ok({"id": pos.id, "sl": pos.sl, "trailing_stop": pos.trailing_stop})


# ---------- 订单 ----------
@router.get("/orders")
async def list_orders(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = _resolve_customer(current, customer_id)
    stmt = select(Order).where(Order.customer_id == cid)
    if status:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.order_by(Order.created_at.desc()).limit(200)
    orders = (await db.execute(stmt)).scalars().all()
    # 关联 KOL 名
    kol_ids = {o.kol_id for o in orders if o.kol_id}
    kols = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()} if kol_ids else {}
    out = []
    for o in orders:
        d = {
            "id": o.id, "kol_id": o.kol_id, "kol_name": kols.get(o.kol_id, ""),
            "exchange": o.exchange, "symbol": o.symbol, "side": o.side, "type": o.type,
            "qty": o.qty, "price": o.price, "status": o.status, "filled_qty": o.filled_qty,
            "filled_price": o.filled_price, "tp_level": o.tp_level, "created_at": o.created_at,
            "filled_at": o.filled_at, "deleted_at": o.deleted_at, "error_msg": o.error_msg,
        }
        out.append(d)
    return ok(out)


@router.post("/orders/delete")
async def delete_order_api(
    body: DeleteOrderRequest,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await order_manager.delete_order(db, body.order_id, current.id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "删除失败"))
    return ok(result)


@router.post("/orders/manual")
async def manual_order(
    body: ManualOrderRequest,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """客户手动下单(非跟单)。"""
    from app.schemas.signal import ParsedSignal
    from app.services import strategy_engine

    parsed = ParsedSignal(
        symbol=body.symbol, side="long" if body.side == "buy" else "short",
        entry_price=body.price, take_profits=body.take_profits, stop_loss=body.stop_loss,
        leverage=body.leverage, raw_text="手动下单",
    )
    decision = strategy_engine.StrategyDecision(allow=True, notional_usdt=body.qty, params={})
    defaults = {
        "default_tp_pct": [0.10, 0.20], "default_sl_pct": -0.05, "no_stop_loss": False,
        "cost_protection_buffer": 0.002, "tp_levels": [], "enable_trailing": False,
        "trailing_callback": 0.01, "batch_entry_enabled": False, "batch_entry_window": 0,
    }
    try:
        result = await order_manager._place_entry(
            db, customer_id=current.id, kol_id=None, signal_id=None,
            exchange=body.exchange, testnet=False, parsed=parsed,
            notional_usdt=body.qty, defaults=defaults,
            market_price=body.price, strategy=None,
        )
        return ok(result)
    except Exception as e:
        raise HTTPException(500, f"手动下单失败: {e}") from e


# ---------- 成交记录 ----------
@router.get("/trades")
async def list_trades(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = _resolve_customer(current, customer_id)
    trades = (
        await db.execute(
            select(Trade).where(Trade.customer_id == cid).order_by(Trade.executed_at.desc()).limit(300)
        )
    ).scalars().all()
    kol_ids = {t.kol_id for t in trades if t.kol_id}
    kols = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()} if kol_ids else {}
    out = []
    for t in trades:
        out.append({
            "id": t.id, "kol_id": t.kol_id, "kol_name": kols.get(t.kol_id, ""),
            "exchange": t.exchange, "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "price": t.price, "realized_pnl": t.realized_pnl, "is_close": t.is_close,
            "tp_level": t.tp_level, "executed_at": t.executed_at,
        })
    return ok(out)


# ===================== 待触发单(限价挂单) =====================


@router.get("/pending-orders")
async def list_pending_orders_api(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """查询待触发单列表。"""
    from app.services import pending_order_manager

    cid = _resolve_customer(current, customer_id)
    data = await pending_order_manager.list_pending_orders(db, cid, status)
    return ok(data)


@router.post("/pending-orders/cancel")
async def cancel_pending_order_api(
    body: dict,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """取消待触发单。"""
    from app.services import pending_order_manager

    pending_id = body.get("pending_id")
    if not pending_id:
        raise HTTPException(400, "缺少 pending_id")
    result = await pending_order_manager.cancel_pending_order(db, int(pending_id), current.id, body.get("reason", ""))
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "取消失败"))
    return ok(result)


# ============ 客户品种倍率 ============

from pydantic import BaseModel


class MultiplierUpdate(BaseModel):
    config_id: int
    multiplier: float


@router.get("/symbol-multipliers")
async def get_my_multipliers(
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """获取所有分类及当前客户的倍率设置(未设置的返回默认值)。"""
    from app.models.symbol_config import SymbolNotionalConfig
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    configs = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.enabled.is_(True)).order_by(SymbolNotionalConfig.id)
    )).scalars().all()

    overrides = {}
    for cid in [c.id for c in configs]:
        cm = (await db.execute(
            select(CustomerSymbolMultiplier).where(
                CustomerSymbolMultiplier.customer_id == current.id,
                CustomerSymbolMultiplier.config_id == cid,
            )
        )).scalar_one_or_none()
        if cm:
            overrides[cid] = cm.multiplier

    return ok([
        {
            "id": c.id,
            "name": c.name,
            "symbols": c.symbols,
            "default_multiplier": c.multiplier,
            "multiplier": overrides.get(c.id, c.multiplier),
            "customer_override": c.id in overrides,
            "note": c.note,
        }
        for c in configs
    ])


@router.post("/symbol-multipliers")
async def set_my_multipliers(
    body: list[MultiplierUpdate],
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """批量更新客户自己的倍率覆盖。"""
    from app.models.symbol_config import SymbolNotionalConfig
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    updated = []
    for item in body:
        if item.multiplier <= 0:
            raise HTTPException(400, f"倍率必须大于 0")
        cfg = (await db.execute(
            select(SymbolNotionalConfig).where(SymbolNotionalConfig.id == item.config_id)
        )).scalar_one_or_none()
        if not cfg:
            raise HTTPException(404, f"分类 {item.config_id} 不存在")
        cm = (await db.execute(
            select(CustomerSymbolMultiplier).where(
                CustomerSymbolMultiplier.customer_id == current.id,
                CustomerSymbolMultiplier.config_id == item.config_id,
            )
        )).scalar_one_or_none()
        if cm:
            cm.multiplier = item.multiplier
        else:
            cm = CustomerSymbolMultiplier(
                customer_id=current.id,
                config_id=item.config_id,
                multiplier=item.multiplier,
            )
            db.add(cm)
        updated.append(item.config_id)

    await db.commit()
    return ok({"updated": updated})


@router.delete("/symbol-multipliers/{config_id}")
async def reset_my_multiplier(
    config_id: int,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """重置单个分类为管理员默认值。"""
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    cm = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.customer_id == current.id,
            CustomerSymbolMultiplier.config_id == config_id,
        )
    )).scalar_one_or_none()
    if cm:
        await db.delete(cm)
        await db.commit()
    return ok({"config_id": config_id})


# ============ 自定义币种倍率 ============

class CustomSymbolCreate(BaseModel):
    symbol: str
    multiplier: float


class CustomSymbolUpdate(BaseModel):
    multiplier: float


@router.get("/custom-symbols")
async def list_custom_symbols(
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """获取当前客户的所有自定义币种倍率。"""
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    rows = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.customer_id == current.id,
            CustomerSymbolMultiplier.custom_symbol.isnot(None),
        ).order_by(CustomerSymbolMultiplier.custom_symbol)
    )).scalars().all()

    return ok([
        {
            "id": r.id,
            "symbol": r.custom_symbol,
            "multiplier": r.multiplier,
        }
        for r in rows
    ])


@router.post("/custom-symbols")
async def add_custom_symbol(
    body: CustomSymbolCreate,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """添加自定义币种倍率。"""
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    symbol = body.symbol.strip().upper()
    if not symbol:
        raise HTTPException(400, "币种不能为空")
    if len(symbol) > 20:
        raise HTTPException(400, "币种名称过长(最多20字符)")
    if body.multiplier <= 0:
        raise HTTPException(400, "倍率必须大于 0")

    existing = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.customer_id == current.id,
            CustomerSymbolMultiplier.custom_symbol == symbol,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"币种 {symbol} 已存在,请编辑或删除后重新添加")

    cm = CustomerSymbolMultiplier(
        customer_id=current.id,
        custom_symbol=symbol,
        multiplier=body.multiplier,
    )
    db.add(cm)
    await db.commit()
    await db.refresh(cm)

    return ok({"id": cm.id, "symbol": cm.custom_symbol, "multiplier": cm.multiplier})


@router.put("/custom-symbols/{item_id}")
async def update_custom_symbol(
    item_id: int,
    body: CustomSymbolUpdate,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """更新自定义币种倍率。"""
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    if body.multiplier <= 0:
        raise HTTPException(400, "倍率必须大于 0")

    cm = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.id == item_id,
            CustomerSymbolMultiplier.customer_id == current.id,
            CustomerSymbolMultiplier.custom_symbol.isnot(None),
        )
    )).scalar_one_or_none()
    if not cm:
        raise HTTPException(404, "自定义币种不存在")

    cm.multiplier = body.multiplier
    await db.commit()
    return ok({"id": cm.id, "symbol": cm.custom_symbol, "multiplier": cm.multiplier})


@router.delete("/custom-symbols/{item_id}")
async def delete_custom_symbol(
    item_id: int,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """删除自定义币种倍率。"""
    from app.models.customer_multiplier import CustomerSymbolMultiplier

    cm = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.id == item_id,
            CustomerSymbolMultiplier.customer_id == current.id,
            CustomerSymbolMultiplier.custom_symbol.isnot(None),
        )
    )).scalar_one_or_none()
    if not cm:
        raise HTTPException(404, "自定义币种不存在")

    await db.delete(cm)
    await db.commit()
    return ok({"id": item_id})
