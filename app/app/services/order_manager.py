"""订单管理服务:信号→风控→策略→下单→持仓跟踪→止盈止损→成本保护→平仓。

核心流程:
1. 接收解析后的信号 + 客户 + KOL
2. 风控校验(授权/静默/上限)
3. 信号过滤与纠错
4. 策略计算仓位(马丁格尔等)
5. 下单(市价/限价,支持分批建仓)
6. 建立持仓记录 + 配置多级止盈/止损
7. 触发成本保护(TP1 或 +2% 后止损上移至入场价+缓冲)
8. 手动平仓 / 删除未成交单
9. 全程事件推送 + 飞书告警
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.models.kol import Kol
from app.models.signal import Signal
from app.models.strategy import Strategy
from app.models.trading import Order, Position, Trade
from app.schemas.signal import ParsedSignal
from app.services import exchange_adapter, signal_filter, strategy_engine
from app.services.authz import has_valid_authorization
from app.services.event_bus import bus
from app.services.notification import notify
from app.services.risk_manager import check_can_trade


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _get_active_master_position(
    db: AsyncSession,
    customer_id: int,
    exchange: str,
    symbol: str,
    side: str,
) -> Position | None:
    """查找指定客户/交易所/品种/方向的活跃主仓位(parent_id IS NULL)。"""
    stmt = select(Position).where(
        Position.customer_id == customer_id,
        Position.exchange == exchange,
        Position.symbol == symbol,
        Position.side == side,
        Position.parent_id.is_(None),
        Position.status == "open",
    )
    return (await db.execute(stmt)).scalars().first()


async def _process_exit_signal(
    db: AsyncSession,
    signal: Signal,
    parsed: ParsedSignal,
    customer_id: int,
    kol_name: str,
) -> dict:
    """处理平仓信号。

    逻辑:
    1. 如果指定了品种和方向 → 平掉该品种该方向的所有持仓
    2. 如果只指定了方向 → 平掉所有该方向的持仓
    3. 如果都没指定 → 平掉所有持仓
    """
    # 获取交易所账号
    ex_acc = await _pick_exchange_account(db, customer_id)
    if not ex_acc:
        await _log_signal_status(db, signal, "rejected", "未配置交易所账号", customer_id)
        return {"ok": False, "reason": "未配置交易所账号"}

    exchange = ex_acc.exchange
    testnet = ex_acc.testnet

    # 查找需要平仓的持仓
    # 只查子仓位(parent_id IS NOT NULL),避免直接操作 master 导致重复平仓
    stmt = select(Position).where(
        Position.customer_id == customer_id,
        Position.exchange == exchange,
        Position.status == "open",
        Position.parent_id.is_not(None),  # 只查子仓位
        Position.kol_id == signal.kol_id,  # 只找该 KOL 的仓位
    )

    # 按品种筛选
    if parsed.symbol:
        stmt = stmt.where(Position.symbol == parsed.symbol)

    # 按方向筛选
    if parsed.side:
        stmt = stmt.where(Position.side == parsed.side)

    positions = (await db.execute(stmt)).scalars().all()

    if not positions:
        # 没有找到该 KOL 的子仓位
        # 安全起见，不自动平掉其他 KOL 的仓位
        # 只记录日志并返回失败
        logger.warning(
            f"平仓信号未找到对应仓位: customer={customer_id}, "
            f"kol_id={signal.kol_id}, symbol={parsed.symbol}, side={parsed.side}"
        )
        await _log_signal_status(
            db, signal, "rejected",
            f"无对应持仓可平（该 KOL 无持仓或已平仓）: {parsed.symbol or '全部'} {parsed.side or '全部方向'}",
            customer_id
        )
        return {"ok": False, "reason": "无对应持仓"}

    # 执行平仓
    closed_positions = []
    total_pnl = 0.0

    for pos in positions:
        try:
            result = await close_position(db, pos.id, pos.qty)
            if result.get("ok"):
                closed_positions.append(pos.id)
                total_pnl += result.get("pnl", 0.0)
        except Exception as e:
            logger.warning(f"平仓失败 position={pos.id}: {e}")

    if not closed_positions:
        await _log_signal_status(db, signal, "rejected", "平仓失败", customer_id)
        return {"ok": False, "reason": "平仓失败"}

    # 更新信号状态
    await _log_signal_status(
        db, signal, "ordered",
        f"平仓成功: {len(closed_positions)} 个持仓, 盈亏 {total_pnl:.2f} USDT",
        customer_id
    )

    # 通知
    await notify(
        "tp_sl", "平仓成功",
        f"KOL {kol_name} 平仓信号\n品种: {parsed.symbol or '全部'}\n方向: {parsed.side or '全部'}\n平仓数: {len(closed_positions)}\n盈亏: {total_pnl:.2f} USDT",
        customer_id
    )

    return {
        "ok": True,
        "reason": f"已平仓 {len(closed_positions)} 个持仓",
        "position_ids": closed_positions,
        "total_pnl": total_pnl,
    }


async def _notional_to_amount(ex, symbol: str, notional_usdt: float, price: float) -> float:
    """将 USDT 名义价值转为合约数量(按交易所精度取整)。"""
    try:
        market = ex.market(symbol)
    except Exception:
        try:
            await ex.load_markets()
            market = ex.market(symbol)
        except Exception:
            market = None
    amount_raw = notional_usdt / price if price > 0 else 0
    if hasattr(ex, "amount_to_precision"):
        try:
            s = ex.amount_to_precision(symbol, amount_raw)
            return float(s)
        except (ValueError, TypeError):
            pass
    return amount_raw


async def _get_symbol_multiplier(db: AsyncSession, customer_id: int, symbol: str) -> float:
    """根据 symbol 查找倍率,优先级: 客户自定义币种 > 客户分类覆盖 > 管理员默认 > 1.0。"""
    from app.models.symbol_config import SymbolNotionalConfig
    from app.models.customer_multiplier import CustomerSymbolMultiplier
    symbol_upper = symbol.upper()

    custom_rows = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.customer_id == customer_id,
            CustomerSymbolMultiplier.custom_symbol.isnot(None),
        )
    )).scalars().all()

    for cr in custom_rows:
        if symbol_upper.startswith(cr.custom_symbol.upper()):
            return cr.multiplier

    rows = (await db.execute(
        select(SymbolNotionalConfig).where(SymbolNotionalConfig.enabled.is_(True))
    )).scalars().all()

    matched = None
    for r in rows:
        prefixes = r.symbol_list()
        if not prefixes:
            matched = r
            break
        for prefix in prefixes:
            if symbol_upper.startswith(prefix):
                matched = r
                break
        if matched:
            break

    if not matched:
        return 1.0

    cm = (await db.execute(
        select(CustomerSymbolMultiplier).where(
            CustomerSymbolMultiplier.customer_id == customer_id,
            CustomerSymbolMultiplier.config_id == matched.id,
        )
    )).scalar_one_or_none()

    return cm.multiplier if cm else matched.multiplier


async def process_signal(
    db: AsyncSession,
    signal: Signal,
    parsed: ParsedSignal,
    customer_id: int,
) -> dict:
    """对单个客户处理一条信号:风控→过滤→策略→下单。

    返回 {ok, reason, order_id?, position_id?}
    """
    kol = (await db.execute(select(Kol).where(Kol.id == signal.kol_id))).scalar_one_or_none()
    kol_name = kol.name if kol else "未知KOL"

    # 0. 检查是否为平仓信号
    if parsed.is_exit_signal:
        logger.info(f"处理平仓信号: customer={customer_id} symbol={parsed.symbol} side={parsed.side}")
        return await _process_exit_signal(db, signal, parsed, customer_id, kol_name)

    # 1. 策略与默认参数
    strategy, notional_override = await strategy_engine.get_strategy_for_follow(db, customer_id, signal.kol_id)
    decision = strategy_engine.compute_decision(strategy)
    # 客户自定义跟单金额覆盖策略中的 base_qty
    if notional_override and notional_override > 0:
        decision.notional_usdt = notional_override

    # 1.1 应用品种分类倍率
    symbol_multiplier = await _get_symbol_multiplier(db, customer_id, parsed.symbol)
    if symbol_multiplier != 1.0:
        decision.notional_usdt = round(decision.notional_usdt * symbol_multiplier, 2)
        logger.info(f"品种分类倍率: symbol={parsed.symbol} multiplier={symbol_multiplier} notional={decision.notional_usdt}")

    defaults = strategy_engine.get_strategy_defaults(decision.params or {})

    # 2. 交易所账号
    ex_acc = await _pick_exchange_account(db, customer_id)
    if not ex_acc:
        return {"ok": False, "reason": "未配置交易所账号"}
    exchange = ex_acc.exchange
    testnet = ex_acc.testnet

    # 3. 策略熔断检查(马丁格尔熔断等)——必须在写去重表之前检查
    if not decision.allow:
        await _log_signal_status(db, signal, "rejected", decision.reason, customer_id)
        return {"ok": False, "reason": decision.reason}

    # 4. 金额上限校验(管理员强制,防共用兜底)——必须在写去重表之前检查
    from app.services.risk_manager import check_order_amount

    amt_ok, amt_reason = await check_order_amount(db, customer_id, decision.notional_usdt, exchange)
    if not amt_ok:
        await _log_signal_status(db, signal, "rejected", f"金额超限: {amt_reason}", customer_id)
        return {"ok": False, "reason": amt_reason}

    # 5. 风控(授权/静默/并发上限)——必须在写去重表之前检查
    can, reason = await check_can_trade(db, customer_id, exchange, parsed.symbol)
    if not can:
        await _log_signal_status(db, signal, "rejected", f"风控拒绝: {reason}", customer_id)
        return {"ok": False, "reason": reason}

    # 6. 获取市价用于纠错
    market_price = await exchange_adapter.fetch_market_price(exchange, parsed.symbol)
    redis = await get_redis()

    # 7. 过滤与纠错(此处才写 Redis 去重表)
    fr = await signal_filter.filter_signal(
        parsed=parsed,
        redis=redis,
        market_price=market_price,
        default_tp_pct=defaults["default_tp_pct"],
        default_sl_pct=defaults["default_sl_pct"],
        no_stop_loss=defaults["no_stop_loss"],
        kol_id=signal.kol_id,
    )
    if not fr.accepted:
        await _log_signal_status(db, signal, fr.decision, fr.reject_reason, customer_id, fr.dedup_hash)
        return {"ok": False, "reason": fr.reject_reason}

    # 8. 下单(智能分流:入场价远离市价时创建待触发单)
    from app.services import pending_order_manager

    if (
        fr.signal.entry_price
        and market_price
        and pending_order_manager.should_use_pending_order(
            fr.signal.entry_price, market_price, fr.signal.side
        )
    ):
        # 入场价远离市价 → 创建待触发单,等待价格触及后再下单
        po_result = await pending_order_manager.create_pending_order(
            db,
            customer_id=customer_id,
            kol_id=signal.kol_id,
            signal_id=signal.id,
            exchange=exchange,
            parsed=fr.signal,
            notional_usdt=decision.notional_usdt,
            defaults=defaults,
            strategy_id=strategy.id if strategy else None,
        )
        if po_result.get("ok"):
            await _log_signal_status(
                db, signal, "ordered",
                f"入场价 {fr.signal.entry_price} 远离市价 {market_price},已创建待触发单(7天过期)",
                customer_id, fr.dedup_hash,
            )
            await notify(
                "order", "信号已挂单等待",
                f"KOL: {kol_name}\n品种: {parsed.symbol}\n方向: {parsed.side}\n"
                f"目标入场价: {fr.signal.entry_price}\n当前市价: {market_price}\n"
                f"等待价格触及后自动下单,7天内有效",
                customer_id,
            )
            return {"ok": True, "pending_id": po_result.get("pending_id"), "reason": "已创建待触发单"}
        else:
            await _log_signal_status(db, signal, "rejected", po_result.get("reason", ""), customer_id, fr.dedup_hash)
            return {"ok": False, "reason": po_result.get("reason", "创建待触发单失败")}

    # 入场价接近市价 → 直接市价下单
    try:
        result = await _place_entry(
            db,
            customer_id=customer_id,
            kol_id=signal.kol_id,
            signal_id=signal.id,
            exchange=exchange,
            testnet=testnet,
            parsed=fr.signal,
            notional_usdt=decision.notional_usdt,
            defaults=defaults,
            market_price=market_price,
            strategy=strategy,
        )
    except Exception as e:
        logger.exception(f"下单失败 customer={customer_id} signal={signal.id}")
        await _log_signal_status(db, signal, "failed", f"下单异常: {e}", customer_id, fr.dedup_hash)
        await notify("error", "下单失败", f"客户{customer_id} {parsed.symbol} 下单异常: {e}", customer_id)
        return {"ok": False, "reason": f"下单异常: {e}"}

    # 6. 更新信号状态
    await _log_signal_status(db, signal, "ordered", fr.correct_log, customer_id, fr.dedup_hash, corrected=fr.decision == "corrected")
    if fr.correct_log:
        await notify("correct", "信号已纠错", f"KOL {kol_name} {parsed.symbol}\n{fr.correct_log}", customer_id)

    # 7. 事件推送
    if result.get("order"):
        await bus.publish_customer(customer_id, "order", result.get("order"))
    await notify(
        "order", "跟单下单",
        f"KOL: {kol_name}\n品种: {parsed.symbol}\n方向: {parsed.side}\n入场: {parsed.entry_price}\n杠杆: {parsed.leverage}x\n名义价值: {decision.notional_usdt} USDT",
        customer_id,
    )
    return {"ok": True, "order_id": result.get("order_id"), "position_id": result.get("position_id")}


async def _pick_exchange_account(db: AsyncSession, customer_id: int):
    from app.models.config import ExchangeAccount

    stmt = select(ExchangeAccount).where(
        ExchangeAccount.customer_id == customer_id,
        ExchangeAccount.is_active.is_(True),
    ).order_by(ExchangeAccount.id)
    return (await db.execute(stmt)).scalars().first()


async def _place_entry(
    db: AsyncSession,
    *,
    customer_id: int,
    kol_id: int,
    signal_id: int,
    exchange: str,
    testnet: bool,
    parsed: ParsedSignal,
    notional_usdt: float,
    defaults: dict,
    market_price: float | None,
    strategy: Strategy | None,
) -> dict:
    """实际下单并建立持仓/订单记录,支持主仓位/子仓位聚合模型。"""
    batch_enabled = defaults.get("batch_entry_enabled", True)
    batch_window = defaults.get("batch_entry_window", 300)

    master = None
    if batch_enabled:
        candidate = await _get_active_master_position(db, customer_id, exchange, parsed.symbol, parsed.side)
        if candidate is not None:
            elapsed = (_utcnow() - candidate.opened_at).total_seconds() if candidate.opened_at else float("inf")
            if elapsed <= batch_window:
                master = candidate
                logger.info(f"分批建仓: 复用主仓 pos_id={candidate.id} elapsed={elapsed:.0f}s window={batch_window}s")
            else:
                logger.info(f"分批建仓窗口已过 ({elapsed:.0f}s > {batch_window}s), 创建新主仓")

    ex, acc = await exchange_adapter.load_exchange(db, customer_id, exchange, testnet)
    try:
        entry_price = parsed.entry_price or market_price
        if not entry_price or entry_price <= 0:
            raise ValueError("无可用入场价")
        amount = await _notional_to_amount(ex, parsed.symbol, notional_usdt, entry_price)
        if amount <= 0:
            raise ValueError("计算仓位为0")

        order_side = "buy" if parsed.side == "long" else "sell"

        if master is None:
            order_type = "market"
            ex_order = await exchange_adapter.place_order(
                ex, parsed.symbol, order_side, order_type, amount, leverage=parsed.leverage
            )

            order = Order(
                customer_id=customer_id,
                kol_id=kol_id,
                signal_id=signal_id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=order_side,
                type=order_type,
                qty=amount,
                price=entry_price,
                leverage=parsed.leverage,
                batch_no=1,
                status="filled",
                exchange_order_id=str(ex_order.get("id", "")),
                filled_qty=float(ex_order.get("filled") or amount),
                filled_price=float(ex_order.get("average") or ex_order.get("price") or entry_price),
                filled_at=_utcnow(),
                created_at=_utcnow(),
            )
            db.add(order)

            real_entry = order.filled_price or entry_price
            tp_levels_cfg = _build_tp_levels(parsed, defaults, real_entry, parsed.side)
            sl = parsed.stop_loss

            master_position = Position(
                customer_id=customer_id,
                kol_id=kol_id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=parsed.side,
                entry_price=real_entry,
                qty=order.filled_qty,
                initial_qty=order.filled_qty,
                tp_levels=tp_levels_cfg,
                sl=sl,
                initial_sl=sl,
                leverage=parsed.leverage,
                cost_protection=False,
                breakeven_moved=False,
                trailing_stop=defaults.get("enable_trailing", False),
                trailing_callback=defaults.get("trailing_callback", 0.0),
                status="open",
                realized_pnl=0.0,
                opened_at=_utcnow(),
            )
            db.add(master_position)
            await db.flush()
            order.position_id = master_position.id

            sub_position = Position(
                customer_id=customer_id,
                kol_id=kol_id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=parsed.side,
                parent_id=master_position.id,
                entry_price=real_entry,
                qty=order.filled_qty,
                initial_qty=order.filled_qty,
                tp_levels=tp_levels_cfg,
                sl=sl,
                initial_sl=sl,
                leverage=parsed.leverage,
                cost_protection=False,
                breakeven_moved=False,
                trailing_stop=defaults.get("enable_trailing", False),
                trailing_callback=defaults.get("trailing_callback", 0.0),
                status="open",
                realized_pnl=0.0,
                opened_at=_utcnow(),
            )
            db.add(sub_position)

            trade = Trade(
                customer_id=customer_id,
                kol_id=kol_id,
                position_id=master_position.id,
                order_id=order.id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=order_side,
                qty=order.filled_qty,
                price=real_entry,
                fee=0.0,
                realized_pnl=0.0,
                is_close=False,
                tp_level=0,
                executed_at=_utcnow(),
            )
            db.add(trade)
            await db.commit()

            return {
                "order_id": order.id,
                "position_id": sub_position.id,
                "order": _order_dict(order, kol_id),
            }
        else:
            # master 已存在,在交易所下单并创建子仓位(分批建仓)
            from sqlalchemy import func as sa_func, or_ as sa_or
            stmt = select(sa_func.count()).where(
                sa_or(
                    Order.position_id == master.id,
                    Order.position_id.in_(
                        select(Position.id).where(Position.parent_id == master.id)
                    ),
                )
            )
            count = (await db.execute(stmt)).scalar() or 0
            next_batch_no = count + 1

            ex_order = await exchange_adapter.place_order(
                ex, parsed.symbol, order_side, "market", amount, leverage=parsed.leverage
            )

            order = Order(
                customer_id=customer_id,
                kol_id=kol_id,
                signal_id=signal_id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=order_side,
                type="market",
                qty=amount,
                price=entry_price,
                leverage=parsed.leverage,
                batch_no=next_batch_no,
                status="filled",
                exchange_order_id=str(ex_order.get("id", "")),
                filled_qty=float(ex_order.get("filled") or amount),
                filled_price=float(ex_order.get("average") or ex_order.get("price") or entry_price),
                filled_at=_utcnow(),
                created_at=_utcnow(),
            )
            db.add(order)

            sub_entry = float(order.filled_price or entry_price)
            sub_tp_levels_cfg = _build_tp_levels(parsed, defaults, sub_entry, parsed.side)
            sub_sl = parsed.stop_loss

            sub_position = Position(
                customer_id=customer_id,
                kol_id=kol_id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=parsed.side,
                parent_id=master.id,
                entry_price=sub_entry,
                qty=order.filled_qty,
                initial_qty=order.filled_qty,
                tp_levels=sub_tp_levels_cfg,
                sl=sub_sl,
                initial_sl=sub_sl,
                leverage=parsed.leverage,
                cost_protection=False,
                breakeven_moved=False,
                trailing_stop=defaults.get("enable_trailing", False),
                trailing_callback=defaults.get("trailing_callback", 0.0),
                status="open",
                realized_pnl=0.0,
                opened_at=_utcnow(),
            )
            db.add(sub_position)
            await db.flush()
            order.position_id = sub_position.id

            trade = Trade(
                customer_id=customer_id,
                kol_id=kol_id,
                position_id=sub_position.id,
                order_id=order.id,
                exchange=exchange,
                symbol=parsed.symbol,
                side=order_side,
                qty=order.filled_qty,
                price=sub_entry,
                fee=0.0,
                realized_pnl=0.0,
                is_close=False,
                tp_level=0,
                executed_at=_utcnow(),
            )
            db.add(trade)

            old_total = master.entry_price * master.qty
            new_total = sub_entry * order.filled_qty
            new_qty = master.qty + order.filled_qty
            new_entry_price = (old_total + new_total) / new_qty if new_qty > 0 else master.entry_price

            master.qty = new_qty
            master.initial_qty += order.filled_qty
            master.entry_price = round(new_entry_price, 8)

            await db.commit()

            return {
                "order_id": order.id,
                "position_id": sub_position.id,
                "order": _order_dict(order, kol_id),
            }
    finally:
        await exchange_adapter.close_exchange(ex)


def _build_tp_levels(parsed: ParsedSignal, defaults: dict, entry: float, side: str) -> list[dict]:
    """构建多级止盈配置 [{level, price, pct, status}]。

    平仓比例规则:
      - defaults["tp_levels"] 形如 [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]]
        第一项是涨幅百分比,第二项是平仓比例
      - 信号提供的止盈价:按位置从 tp_levels 取平仓比例
      - 补充的默认涨幅级:按位置从 tp_levels 取涨幅和平仓比例
    """
    tps = parsed.take_profits or []
    # 从 defaults 读取 tp_levels 配置(优先),否则用默认 [[涨幅, 比例], ...]
    tp_levels_cfg = defaults.get("tp_levels") or [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]]
    # 提取平仓比例列表(第二项)
    close_pcts = [float(x[1]) if len(x) >= 2 else 0.0 for x in tp_levels_cfg]
    # 提取默认涨幅列表(第一项)
    default_tp_pct = [float(x[0]) if len(x) >= 1 else 0.0 for x in tp_levels_cfg]

    levels = []
    # 1. 信号提供的止盈价:按位置从 close_pcts 取比例
    for i, tp in enumerate(tps):
        pct = close_pcts[i] if i < len(close_pcts) else 0.0
        levels.append({
            "level": i + 1,
            "price": float(tp),
            "pct": pct,
            "status": "pending",
        })
    # 2. 补默认涨幅级(若信号止盈数少于配置数,最多5级)
    for j in range(len(levels), len(default_tp_pct)):
        if len(levels) >= 5:
            break
        pct_value = default_tp_pct[j]
        price = entry * (1 + pct_value) if side == "long" else entry * (1 - pct_value)
        close_pct = close_pcts[j] if j < len(close_pcts) else 0.0
        levels.append({
            "level": len(levels) + 1,
            "price": round(price, 8),
            "pct": close_pct,
            "status": "pending",
        })
    # 3. 归一化 pct 使其和为1
    total = sum(l["pct"] for l in levels) or 1
    for l in levels:
        l["pct"] = round(l["pct"] / total, 4)
    return levels


async def _log_signal_status(
    db: AsyncSession,
    signal: Signal,
    status: str,
    note: str,
    customer_id: int,
    dedup_hash: str = "",
    corrected: bool = False,
) -> None:
    signal.status = status
    signal.note = (signal.note + f"\n[客户{customer_id}] {note}").strip()
    if dedup_hash:
        signal.dedup_hash = dedup_hash
    if corrected:
        signal.corrected = True
        signal.correct_log = (signal.correct_log + f"\n[客户{customer_id}] {note}").strip()
    await db.commit()


def _order_dict(order: Order, kol_id: int | None) -> dict:
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "kol_id": kol_id,
        "exchange": order.exchange,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "qty": order.qty,
        "price": order.price,
        "status": order.status,
        "filled_qty": order.filled_qty,
        "filled_price": order.filled_price,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


async def close_position(db: AsyncSession, position_id: int, qty: float | None = None) -> dict:
    """手动(或止盈止损触发)平仓,支持子仓位同步主仓位。"""
    position = (await db.execute(select(Position).where(Position.id == position_id))).scalar_one_or_none()
    if not position or position.status != "open":
        return {"ok": False, "reason": "持仓不存在或已平仓"}
    close_qty = qty if qty and qty > 0 else position.qty
    if close_qty > position.qty:
        close_qty = position.qty

    exchange_position = position
    master = None
    if position.parent_id is not None:
        master = (await db.execute(
            select(Position).where(Position.id == position.parent_id)
        )).scalar_one_or_none()
        if master and master.status == "open":
            exchange_position = master

    ex, _ = await exchange_adapter.load_exchange(db, position.customer_id, position.exchange)
    try:
        ex_order = await exchange_adapter.close_position_market(
            ex, exchange_position.symbol, exchange_position.side, close_qty
        )
        filled = float(ex_order.get("filled") or close_qty)
        fill_price = float(ex_order.get("average") or ex_order.get("price") or 0)

        if position.side == "long":
            pnl = (fill_price - position.entry_price) * filled
        else:
            pnl = (position.entry_price - fill_price) * filled

        order = Order(
            customer_id=position.customer_id,
            kol_id=position.kol_id,
            position_id=position.id,
            exchange=position.exchange,
            symbol=position.symbol,
            side="sell" if position.side == "long" else "buy",
            type="market",
            qty=filled,
            price=fill_price,
            leverage=position.leverage,
            status="filled",
            exchange_order_id=str(ex_order.get("id", "")),
            filled_qty=filled,
            filled_price=fill_price,
            filled_at=_utcnow(),
            created_at=_utcnow(),
            tp_level=-1,
        )
        db.add(order)

        trade = Trade(
            customer_id=position.customer_id,
            kol_id=position.kol_id,
            position_id=position.id,
            order_id=order.id,
            exchange=position.exchange,
            symbol=position.symbol,
            side=order.side,
            qty=filled,
            price=fill_price,
            realized_pnl=pnl,
            is_close=True,
            tp_level=-1,
            executed_at=_utcnow(),
        )
        db.add(trade)

        position.qty -= filled
        position.realized_pnl += pnl
        if position.qty <= 0.0000001:
            position.qty = 0
            position.status = "closed"
            position.closed_at = _utcnow()
            if position.kol_id:
                strat, _ = await strategy_engine.get_strategy_for_follow(db, position.customer_id, position.kol_id)
                if strat:
                    # 用 realized_pnl 判断胜负(包含之前分批止盈的 pnl),而非本次 pnl
                    # 例:TP1 盈利 200,止损亏损 100 → realized_pnl=100 > 0 → won=True
                    await strategy_engine.record_trade_result(db, strat.id, won=position.realized_pnl > 0, qty=position.initial_qty)

        if master is not None and master.status == "open":
            master.qty -= filled
            master.realized_pnl += pnl
            if master.qty <= 0.0000001:
                master.qty = 0
                master.status = "closed"
                master.closed_at = _utcnow()
        elif position.parent_id is None and position.status == "closed":
            # 直接全部平仓 master 仓位时,按子仓位 qty 比例分配 pnl,更新 realized_pnl 并创建 Trade
            # 这样 KOL 排行/胜率统计才能正确归属到各 KOL
            # 注意:master 的 Trade 已在上面创建(realized_pnl=pnl),需置 0 避免利润重复计算
            # 因为子仓位的 Trade 会分配全部 pnl,SUM(Trade.realized_pnl) 仍等于总 pnl
            children = (
                await db.execute(
                    select(Position).where(
                        Position.parent_id == position.id,
                        Position.status == "open",
                    )
                )
            ).scalars().all()
            if children:
                # 有子仓位:master Trade 的 pnl 置 0,由子仓位 Trade 分配全部 pnl
                trade.realized_pnl = 0
                total_child_qty = sum(c.qty for c in children) or 1.0
                order_side_str = "sell" if position.side == "long" else "buy"
                for child in children:
                    child_qty = child.qty
                    child_pnl = pnl * (child_qty / total_child_qty) if total_child_qty > 0 else 0
                    child.qty = 0
                    child.status = "closed"
                    child.closed_at = _utcnow()
                    child.realized_pnl += child_pnl
                    # 子仓位完全平仓时,记录策略交易结果(用于马丁格尔胜率/熔断)
                    if child.kol_id:
                        strat, _ = await strategy_engine.get_strategy_for_follow(db, child.customer_id, child.kol_id)
                        if strat:
                            await strategy_engine.record_trade_result(db, strat.id, won=child.realized_pnl > 0, qty=child.initial_qty)
                    child_trade = Trade(
                        customer_id=child.customer_id,
                        kol_id=child.kol_id,
                        position_id=child.id,
                        order_id=order.id,
                        exchange=child.exchange,
                        symbol=child.symbol,
                        side=order_side_str,
                        qty=child_qty,
                        price=fill_price,
                        realized_pnl=child_pnl,
                        is_close=True,
                        tp_level=-1,
                        executed_at=_utcnow(),
                    )
                    db.add(child_trade)
            # 如果 children 为空(master 无子仓位),保留 master 的 Trade.realized_pnl=pnl,不处理
        # 注意:部分平仓 master(parent_id IS NULL 且 status 仍为 open)不处理子仓位,由调用方负责

        await db.commit()

        await bus.publish_customer(position.customer_id, "position", {"id": position.id, "status": position.status, "pnl": pnl})
        await notify(
            "tp_sl", "平仓成交",
            f"品种: {position.symbol}\n方向: {position.side}\n平仓价: {fill_price}\n数量: {filled}\n盈亏: {pnl:.2f} USDT",
            position.customer_id,
        )
        return {"ok": True, "pnl": pnl, "status": position.status}
    finally:
        await exchange_adapter.close_exchange(ex)


async def delete_order(db: AsyncSession, order_id: int, customer_id: int) -> dict:
    """删除未成交挂单(撤单 + 软删除);已成交单不可删。"""
    order = (
        await db.execute(select(Order).where(Order.id == order_id, Order.customer_id == customer_id))
    ).scalar_one_or_none()
    if not order:
        return {"ok": False, "reason": "订单不存在"}
    if order.deleted_at:
        return {"ok": False, "reason": "订单已删除"}
    if order.status in ("filled", "partial"):
        return {"ok": False, "reason": "已成交订单不可删除,请使用平仓"}

    # 尝试在交易所撤单
    if order.exchange_order_id and order.status == "pending":
        try:
            ex, _ = await exchange_adapter.load_exchange(db, customer_id, order.exchange)
            try:
                await exchange_adapter.cancel_order(ex, order.exchange_order_id, order.symbol)
            finally:
                await exchange_adapter.close_exchange(ex)
        except Exception as e:
            logger.warning(f"交易所撤单失败: {e}")

    order.status = "deleted"
    order.deleted_at = _utcnow()
    await db.commit()
    return {"ok": True}


async def apply_cost_protection(db: AsyncSession, position: Position) -> bool:
    """达到 TP1 或 +2% 利润后,止损上移至入场价+缓冲(成本保护)。返回是否更新。"""
    if position.breakeven_moved or position.status != "open":
        return False
    buffer = 0.002  # 默认缓冲;实际可从策略取
    new_sl = position.entry_price * (1 + buffer) if position.side == "long" else position.entry_price * (1 - buffer)
    position.sl = new_sl
    position.cost_protection = True
    position.breakeven_moved = True
    await db.commit()
    await bus.publish_customer(
        position.customer_id, "position",
        {"id": position.id, "cost_protection": True, "sl": new_sl, "msg": "成本保护已启用:止损上移至入场价"},
    )
    await notify(
        "tp_sl", "成本保护已启用",
        f"品种: {position.symbol}\n止损上移至入场价+缓冲: {new_sl}\n防止盈利单变亏损",
        position.customer_id,
    )
    return True


async def close_at_tp_level(db: AsyncSession, position: Position, level: int, price: float) -> dict:
    """达到某级止盈 → 按比例平仓 + 触发成本保护,支持子仓位聚合。"""

    if position.parent_id is not None:
        master = (await db.execute(
            select(Position).where(Position.id == position.parent_id)
        )).scalar_one_or_none()
        if not master or master.status != "open":
            return {"ok": False, "reason": "主仓位不存在或已平仓"}

        siblings = (await db.execute(
            select(Position).where(
                Position.parent_id == master.id,
                Position.status == "open",
            )
        )).scalars().all()

        hit_siblings: list[tuple[Position, dict, float]] = []
        total_close_qty = 0.0
        for sib in siblings:
            tp_levels = sib.tp_levels or []
            target = next((t for t in tp_levels if t.get("level") == level), None)
            if target and target.get("status") == "pending":
                # close_qty 基于 initial_qty(不是当前 qty),否则分批止盈后子仓位永远无法完全平仓
                # 例: TP1=30%, TP2=30%, TP3=40%
                #   基于 initial_qty: 1.0 → 0.7 → 0.4 → 0.0 ✅
                #   基于 qty:         1.0 → 0.7 → 0.49 → 0.294 ❌ 永远剩余
                close_qty = min(sib.initial_qty * float(target.get("pct", 0)), sib.qty)
                hit_siblings.append((sib, target, close_qty))
                total_close_qty += close_qty

        if not hit_siblings:
            return {"ok": False, "reason": "无同级止盈待平仓的子仓位"}

        ex, _ = await exchange_adapter.load_exchange(db, master.customer_id, master.exchange)
        try:
            ex_order = await exchange_adapter.close_position_market(
                ex, master.symbol, master.side, total_close_qty
            )
            filled = float(ex_order.get("filled") or total_close_qty)
            fill_price = float(ex_order.get("average") or ex_order.get("price") or price)

            if master.side == "long":
                total_pnl = (fill_price - master.entry_price) * filled
            else:
                total_pnl = (master.entry_price - fill_price) * filled

            result = {"ok": True, "pnl": total_pnl, "status": master.status}

            actual_total_pnl = 0.0  # 实际总盈亏(基于子仓位 entry_price 之和)
            for sib, target, close_qty in hit_siblings:
                # sib_pnl 基于子仓位自己的 entry_price 计算(不用 master.entry_price 按比例分配)
                # 因为各子仓位 entry_price 可能不同,按比例分配会扭曲单个 KOL 的盈亏
                if sib.side == "long":
                    sib_pnl = (fill_price - sib.entry_price) * close_qty
                else:
                    sib_pnl = (sib.entry_price - fill_price) * close_qty
                actual_total_pnl += sib_pnl

                tp_levels = sib.tp_levels or []
                for t in tp_levels:
                    if t.get("level") == level:
                        t["status"] = "hit"
                sib.tp_levels = tp_levels
                sib.qty -= close_qty
                sib.realized_pnl += sib_pnl
                if sib.qty <= 0.0000001:
                    sib.qty = 0
                    sib.status = "closed"
                    sib.closed_at = _utcnow()
                    # 子仓位完全平仓时,记录策略交易结果(用于马丁格尔胜率/熔断)
                    if sib.kol_id:
                        strat, _ = await strategy_engine.get_strategy_for_follow(db, sib.customer_id, sib.kol_id)
                        if strat:
                            await strategy_engine.record_trade_result(db, strat.id, won=sib.realized_pnl > 0, qty=sib.initial_qty)

                order_side = "sell" if sib.side == "long" else "buy"
                trade = Trade(
                    customer_id=sib.customer_id,
                    kol_id=sib.kol_id,
                    position_id=sib.id,
                    order_id=None,
                    exchange=sib.exchange,
                    symbol=sib.symbol,
                    side=order_side,
                    qty=close_qty,
                    price=fill_price,
                    realized_pnl=sib_pnl,
                    is_close=True,
                    tp_level=level,
                    executed_at=_utcnow(),
                )
                db.add(trade)

            master.qty -= filled
            master.realized_pnl += actual_total_pnl  # 用实际子仓位 pnl 之和,确保 master.realized_pnl == sum(sub.realized_pnl)
            if master.qty <= 0.0000001:
                master.qty = 0
                master.status = "closed"
                master.closed_at = _utcnow()

            await db.commit()

            if level == 1:
                for sib, _, _ in hit_siblings:
                    await apply_cost_protection(db, sib)

            await bus.publish_customer(master.customer_id, "position", {
                "id": position.id,
                "status": position.status,
                "pnl": actual_total_pnl,
            })
            await notify(
                "tp_sl", f"第{level}止盈达成(聚合)",
                f"品种: {master.symbol}\n方向: {master.side}\n平仓价: {fill_price}\n聚合数量: {filled}\n盈亏: {actual_total_pnl:.2f} USDT\n涉及子仓位: {len(hit_siblings)}",
                master.customer_id,
            )
            result["pnl"] = actual_total_pnl  # 用实际值覆盖估算值
            return result
        finally:
            await exchange_adapter.close_exchange(ex)
    else:
        tp_levels = position.tp_levels or []
        target = next((t for t in tp_levels if t.get("level") == level), None)
        if not target or target.get("status") != "pending":
            return {"ok": False, "reason": "该止盈级别不可用"}
        close_qty = min(position.initial_qty * float(target.get("pct", 0)), position.qty)
        result = await close_position(db, position.id, close_qty)
        if result.get("ok"):
            target["status"] = "hit"
            position.tp_levels = tp_levels
            if level == 1:
                await apply_cost_protection(db, position)
            await db.commit()
            await notify(
                "tp_sl", f"第{level}止盈达成",
                f"品种: {position.symbol}\n平仓比例: {target.get('pct')}\n平仓价: {price}\n盈亏: {result.get('pnl', 0):.2f}",
                position.customer_id,
            )
        return result
