"""交易路由(客户视图):KOL 跟随、持仓、订单、成交、手动平仓/删除/下单、止损修改。"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_customer, require_admin
from app.models.kol import Kol, KolFollow
from app.models.strategy import Strategy
from app.models.trading import Order, Position, Trade
from app.models.config import ExchangeAccount
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

# ---------- 交易所白名单 ----------
ALLOWED_EXCHANGES = {"okx", "binance", "bybit"}


class CancelPendingOrderRequest(BaseModel):
    pending_id: int
    reason: str = ""


def _account_display_name(acc: ExchangeAccount | None) -> str:
    if not acc:
        return ""
    label = (acc.label or "").strip()
    mode = getattr(acc, "account_mode", "") or ("testnet" if acc.testnet else "live")
    base = label or f"{acc.exchange.upper()} {mode}"
    return f"{base} #{acc.id}"


async def _exchange_account_map(db: AsyncSession, account_ids: set[int | None]) -> dict[int, ExchangeAccount]:
    ids = {int(i) for i in account_ids if i}
    if not ids:
        return {}
    rows = (
        await db.execute(select(ExchangeAccount).where(ExchangeAccount.id.in_(ids)))
    ).scalars().all()
    return {r.id: r for r in rows}


def _attach_account_meta(d: dict, acc: ExchangeAccount | None) -> dict:
    d["exchange_account_label"] = (acc.label or "") if acc else ""
    d["exchange_account_name"] = _account_display_name(acc)
    d["exchange_account_mode"] = getattr(acc, "account_mode", "") if acc else ""
    return d


def _resolve_customer(current, customer_id: int | None) -> int:
    """客户只能查自己;管理员可指定。"""
    if current.role == "customer":
        return current.id
    if customer_id:
        return customer_id
    raise HTTPException(400, "管理员需指定 customer_id")


async def _build_follow_status(db: AsyncSession, customer_id: int, follow: KolFollow) -> dict:
    """构建 KOL 跟随状态:正常/暂停/冷却。

    冷却状态在仪表盘上做 KOL 级提醒；实际下单仍在 order_manager 中按币种+方向精确判断。
    """
    now = datetime.now(timezone.utc)
    paused_until = follow.paused_until
    cooldown_reset_at = getattr(follow, "cooldown_reset_at", None)
    is_paused = bool(paused_until and paused_until > now)

    # 读取可配置冷却时长
    from app.models.config import RiskConfig
    _rc = (await db.execute(
        select(RiskConfig).where(RiskConfig.customer_id == customer_id, RiskConfig.enabled.is_(True))
    )).scalar_one_or_none()
    if _rc and _rc.cooldown_minutes is not None:
        cooldown_minutes = _rc.cooldown_minutes
    else:
        cooldown_minutes = 60

    if cooldown_minutes <= 0:
        # 冷却已禁用
        recent_pos = None
        cooldown_until = None
    else:
        cooldown_since = now - timedelta(minutes=cooldown_minutes)
        if cooldown_reset_at and cooldown_reset_at > cooldown_since:
            cooldown_since = cooldown_reset_at

        recent_pos = (
            await db.execute(
                select(Position)
                .where(
                    Position.customer_id == customer_id,
                    Position.kol_id == follow.kol_id,
                    Position.opened_at >= cooldown_since,
                    Position.source != "pending_trigger",
                )
                .order_by(Position.opened_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        cooldown_until = (recent_pos.opened_at + timedelta(minutes=cooldown_minutes)) if recent_pos else None
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
        # 不只查 enabled=True：风控暂停会把 enabled 置 False，但仍需要在订阅 KOL 中展示并允许恢复。
        rows = (await db.execute(select(KolFollow).where(KolFollow.customer_id == cid))).scalars().all()
        # 预加载策略(用于解析跟单金额默认值)
        strategy_ids = {f.strategy_id for f in rows if f.strategy_id}
        strategy_map: dict[int, float] = {}
        if strategy_ids:
            strats = (await db.execute(select(Strategy).where(Strategy.id.in_(strategy_ids)))).scalars().all()
            for s in strats:
                strategy_map[s.id] = (s.params or {}).get("base_qty", 100.0)
        for f in rows:
            follow_status = await _build_follow_status(db, cid, f)
            if not f.enabled and follow_status["status"] != "paused":
                continue
            followed_ids.add(f.kol_id)
            # 解析跟单金额: 自定义 > 策略 base_qty > 系统默认 100
            resolved_notional = f.followed_notional_usdt
            if not resolved_notional:
                resolved_notional = strategy_map.get(f.strategy_id, 100.0) if f.strategy_id else 100.0
            follow_map[f.kol_id] = {
                "strategy_id": f.strategy_id,
                "notional_usdt": resolved_notional,
                "raw_notional_usdt": f.followed_notional_usdt,
                "notional_source": "custom" if f.followed_notional_usdt else ("strategy" if f.strategy_id else "default"),
                "enabled": f.enabled,
                "paused_until": f.paused_until,
                "cooldown_reset_at": getattr(f, "cooldown_reset_at", None),
                "follow_status": follow_status,
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

    # ★ P2 修复: 验证 KOL 存在且活跃(enabled=True)
    if target_ids:
        active_kols = (await db.execute(
            select(Kol).where(Kol.id.in_(target_ids))
        )).scalars().all()
        active_kol_ids = {k.id for k in active_kols}
        inactive_kols = [k for k in active_kols if not k.enabled]
        not_found_ids = target_ids - active_kol_ids
        if not_found_ids:
            raise HTTPException(400, f"以下 KOL 不存在: {not_found_ids}")
        if inactive_kols:
            inactive_names = [k.name for k in inactive_kols]
            raise HTTPException(400, f"以下 KOL 已停用,无法跟随: {inactive_names}")
        # S16v2: 验证 KOL Discord 频道绑定 (discord_account_id 可选,None=使用默认账号)
        no_discord_kols = [k for k in active_kols if k.enabled and not k.discord_channel_id]
        if no_discord_kols:
            no_discord_names = [k.name for k in no_discord_kols]
            raise HTTPException(400, f"以下 KOL 未绑定 Discord 频道,无法跟单: {no_discord_names}")

    # ★ 安全修复: 验证 strategy_id 归属当前客户,防止跨客户引用
    all_strategy_ids = {s["strategy_id"] for s in settings_map.values() if s.get("strategy_id")}
    if all_strategy_ids:
        valid_strats = (await db.execute(
            select(Strategy).where(
                Strategy.id.in_(all_strategy_ids),
                Strategy.customer_id == cid,
            )
        )).scalars().all()
        valid_strat_ids = {s.id for s in valid_strats}
        invalid_strat_ids = all_strategy_ids - valid_strat_ids
        if invalid_strat_ids:
            raise HTTPException(400, f"以下策略不存在或不属于当前客户: {invalid_strat_ids}")

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
            f.paused_until = None  # P0-2修复: 取消关注时清理暂停状态,防止check_kol_can_trade误自动恢复

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("设置KOL跟随失败")
        raise HTTPException(500, "设置跟随失败,请稍后重试")
    return ok({"followed": list(target_ids)})


@router.post("/kols/{kol_id}/resume")
async def resume_kol_follow(
    kol_id: int,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """恢复 KOL 跟随:清除暂停,并把冷却检查重置到当前时间。"""
    follow = (
        await db.execute(
            select(KolFollow).where(KolFollow.customer_id == current.id, KolFollow.kol_id == kol_id)
        )
    ).scalar_one_or_none()
    if not follow:
        raise HTTPException(404, "未订阅该 KOL")

    now = datetime.now(timezone.utc)
    follow.enabled = True
    follow.paused_until = None
    follow.cooldown_reset_at = now
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("恢复KOL跟随失败")
        raise HTTPException(500, "恢复跟随失败,请稍后重试")
    return ok({"kol_id": kol_id, "status": "active", "cooldown_reset_at": now})


# ---------- 持仓 ----------
@router.get("/positions")
async def list_positions(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    exchange_account_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = current.id if current.role == "customer" else customer_id
    # 持仓管理只展示未结束的子仓位；已平仓仓位应进入历史记录。
    # master 仓位仅作为内部聚合记录，不直接返回给前端。
    # 管理员未指定 customer_id 时返回所有客户的持仓。
    stmt = (
        select(Position)
        .where(
            Position.status == "open",
            Position.parent_id.is_not(None),
        )
        .order_by(Position.opened_at.desc())
    )
    if cid is not None:
        stmt = stmt.where(Position.customer_id == cid)
    # M1修复: 条件追加筛选,避免表达式优先级BUG(None时 == True 变为 == 1)
    if exchange_account_id:
        stmt = stmt.where(Position.exchange_account_id == exchange_account_id)
    positions = (await db.execute(stmt)).scalars().all()
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

    account_map = await _exchange_account_map(db, {p.exchange_account_id for p in positions})
    out = []
    for p in positions:
        price = price_cache.get((p.exchange, p.symbol), 0.0) if p.status == "open" else 0.0
        d = await position_manager.enrich_position(p, price, kol_map.get(p.kol_id, ""))
        out.append(_attach_account_meta(d, account_map.get(p.exchange_account_id)))
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
    # ★ P2 修复: 止损参数范围校验
    if body.sl is not None and body.sl <= 0:
        raise HTTPException(400, "止损价必须大于0")
    if body.trailing_callback is not None and not (0 < body.trailing_callback <= 0.5):
        raise HTTPException(400, "回撤比例需在0~50%之间")
    if body.sl is not None:
        pos.sl = body.sl
    if body.trailing_stop is not None:
        pos.trailing_stop = body.trailing_stop
    if body.trailing_callback is not None:
        pos.trailing_callback = body.trailing_callback
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新止损失败")
        raise HTTPException(500, "更新止损失败,请稍后重试")
    return ok({"id": pos.id, "sl": pos.sl, "trailing_stop": pos.trailing_stop})


# ---------- 订单 ----------
@router.get("/orders")
async def list_orders(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    status: str | None = Query(None),
    exchange_account_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cid = current.id if current.role == "customer" else customer_id
    stmt = select(Order).where(Order.status != "deleted")
    if cid is not None:
        stmt = stmt.where(Order.customer_id == cid)
    if status:
        stmt = stmt.where(Order.status == status)
    if exchange_account_id:
        stmt = stmt.where(Order.exchange_account_id == exchange_account_id)
    stmt = stmt.order_by(Order.created_at.desc()).limit(200)
    orders = (await db.execute(stmt)).scalars().all()
    # 关联 KOL 名
    kol_ids = {o.kol_id for o in orders if o.kol_id}
    kols = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()} if kol_ids else {}
    account_map = await _exchange_account_map(db, {o.exchange_account_id for o in orders})
    out = []
    for o in orders:
        d = {
            "id": o.id, "kol_id": o.kol_id, "kol_name": kols.get(o.kol_id, ""),
            "exchange_account_id": o.exchange_account_id,
            "exchange": o.exchange, "symbol": o.symbol, "side": o.side, "type": o.type,
            "qty": o.qty, "price": o.price, "status": o.status, "filled_qty": o.filled_qty,
            "filled_price": o.filled_price, "tp_level": o.tp_level, "created_at": o.created_at,
            "filled_at": o.filled_at, "deleted_at": o.deleted_at, "error_msg": o.error_msg,
        }
        out.append(_attach_account_meta(d, account_map.get(o.exchange_account_id)))
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

    # ★ P2 修复: 交易所白名单校验(大小写不敏感)
    if body.exchange.lower() not in ALLOWED_EXCHANGES:
        raise HTTPException(400, f"不支持的交易所: {body.exchange}, 仅支持: {', '.join(sorted(ALLOWED_EXCHANGES))}")

    parsed = ParsedSignal(
        symbol=body.symbol, side="long" if body.side == "buy" else "short",
        entry_price=body.price, take_profits=body.take_profits, stop_loss=body.stop_loss,
        leverage=body.leverage, raw_text="手动下单",
    )
    # ★ 修复: 手动下单必须经过风控检查
    from app.models.config import RiskConfig
    from sqlalchemy import select as _sel
    _rc = (await db.execute(
        _sel(RiskConfig).where(RiskConfig.customer_id == current.id, RiskConfig.enabled.is_(True))
    )).scalars().first()
    _defaults = {
        "default_tp_pct": [0.10, 0.20], "default_sl_pct": -0.05, "no_stop_loss": False,
        "cost_protection_buffer": 0.002, "tp_levels": [], "enable_trailing": False,
        "trailing_callback": 0.01, "batch_entry_enabled": False, "batch_entry_window": 0,
    }
    if _rc:
        _defaults.update({
            "default_tp_pct": getattr(_rc, "default_tp_pct", None) or [0.10, 0.20],
            "default_sl_pct": getattr(_rc, "default_sl_pct", None) if getattr(_rc, "default_sl_pct", None) is not None else -0.05,
            "no_stop_loss": getattr(_rc, "no_stop_loss", False),
            "cost_protection_buffer": getattr(_rc, "cost_protection_buffer", None) or 0.002,
            "enable_trailing": getattr(_rc, "enable_trailing", getattr(_rc, "enable_trailing_stop", False)),
            "trailing_callback": (
                (float(getattr(_rc, "trailing_callback_pct", 0) or 0) / 100.0)
                if getattr(_rc, "trailing_callback_pct", None) is not None
                else (getattr(_rc, "trailing_callback", None) or 0.01)
            ),
            "batch_entry_enabled": getattr(_rc, "batch_entry_enabled", False),
            "batch_entry_window": getattr(_rc, "batch_entry_window", None) or 0,
        })
    # ★ 检查急停状态
    if getattr(current, "emergency_stop", False):
        raise HTTPException(403, "急停已激活,无法下单")
    # ★ 检查授权状态
    from app.models.config import ExchangeAccount
    from sqlalchemy import select as _sel2
    _acc = (await db.execute(
        _sel2(ExchangeAccount).where(
            ExchangeAccount.customer_id == current.id,
            ExchangeAccount.exchange == body.exchange,
            ExchangeAccount.is_active.is_(True),
        )
        .order_by(
            ExchangeAccount.is_default.desc(),
            ExchangeAccount.last_error.asc(),
            ExchangeAccount.last_verified_at.desc().nullslast(),
            ExchangeAccount.id,
        )
    )).scalars().first()
    if not _acc:
        raise HTTPException(403, f"交易所 {body.exchange} 未授权或未配置")
    if _acc.last_error:
        raise HTTPException(400, "默认/候选交易所 API 最近验证失败,请先测试连接成功后再下单")
    from datetime import datetime, timezone
    auth_expires_at = getattr(_acc, "auth_expires_at", None)
    if auth_expires_at and auth_expires_at < datetime.now(timezone.utc):
        raise HTTPException(403, "交易所授权已过期")
    # ★ 金额上限检查
    max_notional = getattr(_rc, "max_notional_per_order", None) if _rc else None
    if max_notional is None and _rc:
        max_notional = getattr(_rc, "max_position_usdt", 0)
    # 也检查客户级单笔下单限制
    customer_max_order = getattr(current, "max_order_usdt", None)
    if customer_max_order and customer_max_order > 0:
        if max_notional is None or max_notional <= 0:
            max_notional = customer_max_order
        else:
            max_notional = min(max_notional, customer_max_order)
    if max_notional is not None and max_notional > 0 and body.qty > max_notional:
        raise HTTPException(400, f"下单金额超限: 最大 {max_notional} USDT")
    decision = strategy_engine.StrategyDecision(allow=True, notional_usdt=body.qty, params={})

    # ★ P1 修复: 添加详细日志记录(下单前)
    logger.info(
        f"手动下单请求: customer_id={current.id}, username={current.username}, "
        f"symbol={body.symbol}, side={body.side}, qty={body.qty}, price={body.price}, "
        f"exchange={body.exchange}, leverage={body.leverage}, "
        f"take_profits={body.take_profits}, stop_loss={body.stop_loss}"
    )

    try:
        result = await order_manager._place_entry(
            db, customer_id=current.id, kol_id=None, signal_id=None,
            exchange=body.exchange, testnet=_acc.testnet,
            exchange_account_id=_acc.id,
            parsed=parsed,
            notional_usdt=body.qty, defaults=_defaults,
            market_price=body.price, strategy=None,
        )
        # ★ P1 修复: 添加详细日志记录(下单成功)
        logger.info(
            f"手动下单成功: customer_id={current.id}, symbol={body.symbol}, "
            f"side={body.side}, qty={body.qty}, price={body.price}, "
            f"exchange={body.exchange}, result={result}"
        )
        return ok(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"手动下单失败: customer_id={current.id}, symbol={body.symbol}, "
            f"side={body.side}, qty={body.qty}, price={body.price}, "
            f"exchange={body.exchange}"
        )
        raise HTTPException(500, "手动下单失败,请稍后重试") from e


# ---------- 成交记录 ----------

@router.get("/daily-stats")
async def daily_stats(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    exchange_account_id: int | None = Query(None),
    month: str = Query(..., description="YYYY-MM"),
    db: AsyncSession = Depends(get_db),
):
    """按北京时间自然日统计每日交易数据。"""
    # P0-4修复: 管理员必须传 customer_id,否则返回 400(原代码用 admin User.id 查 Customer.id 字段)
    if current.role == "admin" and customer_id is None:
        raise HTTPException(400, "管理员查询统计需要指定 customer_id")
    cid = _resolve_customer(current, customer_id)

    try:
        parts = month.split("-")
        year, mon = int(parts[0]), int(parts[1])
    except (ValueError, IndexError, AttributeError):
        raise HTTPException(400, "month 格式应为 YYYY-MM")

    beijing_tz = timezone(timedelta(hours=8))
    month_start = datetime(year, mon, 1, tzinfo=beijing_tz)
    if mon == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=beijing_tz)
    else:
        month_end = datetime(year, mon + 1, 1, tzinfo=beijing_tz)

    month_start_utc = month_start.astimezone(timezone.utc)
    month_end_utc = month_end.astimezone(timezone.utc)

    stmt = select(Trade).where(
        Trade.customer_id == cid,
        Trade.executed_at >= month_start_utc,
        Trade.executed_at < month_end_utc,
    )
    if exchange_account_id:
        stmt = stmt.where(Trade.exchange_account_id == exchange_account_id)

    trades = (await db.execute(stmt.order_by(Trade.executed_at.asc()))).scalars().all()

    day_data: dict[str, dict] = defaultdict(lambda: {
        "pnl": 0.0, "trade_count": 0, "open_count": 0, "close_count": 0,
        "fee": 0.0, "win_count": 0, "loss_count": 0,
        "symbols": defaultdict(lambda: {
            "pnl": 0.0, "trade_count": 0, "open_count": 0, "close_count": 0, "fee": 0.0
        })
    })

    for t in trades:
        beijing_time = t.executed_at.astimezone(beijing_tz)
        day_str = beijing_time.strftime("%Y-%m-%d")

        d = day_data[day_str]
        d["pnl"] += t.realized_pnl
        d["trade_count"] += 1
        d["fee"] += t.fee
        if t.is_close:
            d["close_count"] += 1
            if t.realized_pnl > 0:
                d["win_count"] += 1
            elif t.realized_pnl < 0:
                d["loss_count"] += 1
        else:
            d["open_count"] += 1

        s = d["symbols"][t.symbol]
        s["pnl"] += t.realized_pnl
        s["trade_count"] += 1
        s["fee"] += t.fee
        if t.is_close:
            s["close_count"] += 1
        else:
            s["open_count"] += 1

    days = []
    total_pnl = 0.0
    total_trade_count = 0
    total_close_count = 0
    total_fee = 0.0
    total_win = 0
    total_loss = 0

    for day_str in sorted(day_data.keys()):
        d = day_data[day_str]
        symbols = [
            {"symbol": sym, **data}
            for sym, data in sorted(d["symbols"].items())
        ]
        days.append({
            "day": day_str,
            "pnl": round(d["pnl"], 8),
            "trade_count": d["trade_count"],
            "open_count": d["open_count"],
            "close_count": d["close_count"],
            "fee": round(d["fee"], 8),
            "win_count": d["win_count"],
            "loss_count": d["loss_count"],
            "symbols": symbols,
        })
        total_pnl += d["pnl"]
        total_trade_count += d["trade_count"]
        total_close_count += d["close_count"]
        total_fee += d["fee"]
        total_win += d["win_count"]
        total_loss += d["loss_count"]

    win_rate = round(total_win / total_close_count * 100, 1) if total_close_count > 0 else 0

    return ok({
        "days": days,
        "summary": {
            "total_pnl": round(total_pnl, 8),
            "trade_count": total_trade_count,
            "close_count": total_close_count,
            "win_rate": win_rate,
            "fee": round(total_fee, 8),
        }
    })


@router.get("/trades")
async def list_trades(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    exchange_account_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    cid = current.id if current.role == "customer" else customer_id
    # ★ P3 修复: 添加分页参数,避免一次性返回过多数据
    offset = (page - 1) * page_size
    stmt = select(Trade)
    if cid is not None:
        stmt = stmt.where(Trade.customer_id == cid)
    if exchange_account_id:
        stmt = stmt.where(Trade.exchange_account_id == exchange_account_id)
    trades = (
        await db.execute(
            stmt.order_by(Trade.executed_at.desc()).offset(offset).limit(page_size)
        )
    ).scalars().all()
    kol_ids = {t.kol_id for t in trades if t.kol_id}
    kols = {k.id: k.name for k in (await db.execute(select(Kol).where(Kol.id.in_(kol_ids)))).scalars().all()} if kol_ids else {}
    account_map = await _exchange_account_map(db, {t.exchange_account_id for t in trades})
    out = []
    for t in trades:
        d = {
            "id": t.id, "kol_id": t.kol_id, "kol_name": kols.get(t.kol_id, ""),
            "exchange_account_id": t.exchange_account_id,
            "exchange": t.exchange, "symbol": t.symbol, "side": t.side, "qty": t.qty,
            "price": t.price, "fee": t.fee, "realized_pnl": t.realized_pnl, "is_close": t.is_close,
            "tp_level": t.tp_level, "executed_at": t.executed_at,
        }
        out.append(_attach_account_meta(d, account_map.get(t.exchange_account_id)))
    return ok(out)


# ===================== 待触发单(限价挂单) =====================


@router.get("/pending-orders")
async def list_pending_orders_api(
    current=Depends(get_current_user),
    customer_id: int | None = Query(None),
    status: str | None = Query(None),
    exchange_account_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """查询待触发单列表。"""
    from app.services import pending_order_manager

    cid = current.id if current.role == "customer" else customer_id
    data = await pending_order_manager.list_pending_orders(db, cid, status)
    if exchange_account_id:
        data = [p for p in data if p.get("exchange_account_id") == exchange_account_id]
    account_map = await _exchange_account_map(db, {p.get("exchange_account_id") for p in data})
    for p in data:
        _attach_account_meta(p, account_map.get(p.get("exchange_account_id")))
    return ok(data)


@router.post("/pending-orders/cancel")
async def cancel_pending_order_api(
    body: CancelPendingOrderRequest,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """取消待触发单。"""
    from app.services import pending_order_manager

    result = await pending_order_manager.cancel_pending_order(db, body.pending_id, current.id, body.reason)
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

    # ★ P3 修复: 批量查询消除 N+1
    config_ids = [c.id for c in configs]
    overrides = {}
    if config_ids:
        cms = (await db.execute(
            select(CustomerSymbolMultiplier).where(
                CustomerSymbolMultiplier.customer_id == current.id,
                CustomerSymbolMultiplier.config_id.in_(config_ids),
            )
        )).scalars().all()
        overrides = {cm.config_id: cm.multiplier for cm in cms}

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

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新品种倍率失败")
        raise HTTPException(500, "更新倍率失败,请稍后重试")
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
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("重置品种倍率失败")
            raise HTTPException(500, "重置倍率失败,请稍后重试")
    return ok({"config_id": config_id})


# ============ 自定义币种倍率 ============

class CustomSymbolCreate(BaseModel):
    symbol: str
    multiplier: float


class CustomSymbolUpdate(BaseModel):
    multiplier: float




# ==================== 品种分类管理 (客户可增删改) ====================

class CategoryCreate(BaseModel):
    name: str
    symbols: str = ""
    multiplier: float = 1.0
    note: str = ""


class CategoryUpdate(BaseModel):
    name: str | None = None
    symbols: str | None = None
    multiplier: float | None = None
    note: str | None = None


@router.post("/symbol-categories")
async def create_symbol_category(
    body: CategoryCreate,
    current=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """创建品种分类。"""
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
        enabled=True,
        note=body.note,
    )
    db.add(cfg)
    try:
        await db.commit()
        await db.refresh(cfg)
    except Exception:
        await db.rollback()
        logger.exception("创建品种分类失败")
        raise HTTPException(500, "创建品种分类失败")
    # 刷新币种分类缓存
    from app.services.signal_filter import refresh_coin_tier_cache
    await refresh_coin_tier_cache(db)

    return ok({"id": cfg.id, "name": cfg.name})


@router.put("/symbol-categories/{cfg_id}")
async def update_symbol_category(
    cfg_id: int,
    body: CategoryUpdate,
    current=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """更新品种分类。"""
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
    if body.note is not None:
        cfg.note = body.note
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新品种分类失败")
        raise HTTPException(500, "更新品种分类失败")
    # 刷新币种分类缓存
    from app.services.signal_filter import refresh_coin_tier_cache
    await refresh_coin_tier_cache(db)

    return ok({"id": cfg_id})


@router.delete("/symbol-categories/{cfg_id}")
async def delete_symbol_category(
    cfg_id: int,
    current=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除品种分类。同时清除该分类的客户倍率覆盖。"""
    from app.models.symbol_config import SymbolNotionalConfig
    from app.models.customer_multiplier import CustomerSymbolMultiplier
    cfg = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.id == cfg_id)
    )).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "分类不存在")
    cms = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.config_id == cfg_id
        )
    )).scalars().all()
    for cm in cms:
        await db.delete(cm)
    await db.delete(cfg)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除品种分类失败")
        raise HTTPException(500, "删除品种分类失败")
    # 刷新币种分类缓存
    from app.services.signal_filter import refresh_coin_tier_cache
    await refresh_coin_tier_cache(db)

    return ok({"id": cfg_id})


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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("添加自定义币种失败")
        raise HTTPException(500, "添加自定义币种失败,请稍后重试")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新自定义币种失败")
        raise HTTPException(500, "更新自定义币种失败,请稍后重试")
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
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除自定义币种失败")
        raise HTTPException(500, "删除自定义币种失败,请稍后重试")
    return ok({"id": item_id})
