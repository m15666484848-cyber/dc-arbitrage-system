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

from datetime import datetime, timedelta, timezone

import copy
import contextvars

from typing import Any

from loguru import logger

from sqlalchemy import select, func, text

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis

from app.models.kol import Kol, KolFollow

from app.models.referral import ReferralCommission

from app.models.signal import Signal

from app.models.strategy import Strategy

from app.models.trading import Order, Position, Trade

from app.models.customer import Customer

from app.schemas.signal import ParsedSignal

from app.services import exchange_adapter, risk_manager, signal_filter, strategy_engine

from app.services.authz import has_valid_authorization

from app.services.event_bus import bus

from app.services.notification import notify

from app.services.risk_manager import check_can_trade

# 邀请佣金比例:下级正盈利的 10% 作为邀请人佣金

REFERRAL_COMMISSION_RATE = 0.1

# 多 API 跟单上下文:复用原单账号流程时强制选择指定 API。
_forced_exchange_account_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "forced_exchange_account_id", default=None
)

# P3-4: 策略配置缓存(进程级,300秒TTL),避免策略修改后不生效直到重启

_strategy_config_cache: dict[tuple[int, int], tuple[Any, float | None, float]] = {}

_STRATEGY_CONFIG_CACHE_TTL = 300.0

async def _get_cached_strategy_for_follow(

    db: AsyncSession, customer_id: int, kol_id: int

) -> tuple[Any, float | None]:

    '''带TTL的策略配置缓存,策略修改后最多300秒生效。

    注意: 缓存 strategy 对象的 martingale_round/last_result 等状态可能过期,

    但 record_trade_result 使用 with_for_update 从数据库读取最新状态,

    因此状态更新不受缓存影响。

    '''

    now = datetime.now(timezone.utc).timestamp()

    cache_key = (customer_id, kol_id)

    cached = _strategy_config_cache.get(cache_key)

    if cached:

        strategy, notional, ts = cached

        if now - ts < _STRATEGY_CONFIG_CACHE_TTL:

            return strategy, notional

    strategy, notional = await strategy_engine.get_strategy_for_follow(db, customer_id, kol_id)

    _strategy_config_cache[cache_key] = (strategy, notional, now)

    return strategy, notional

def _invalidate_strategy_cache(customer_id: int, kol_id: int | None) -> None:

    '''策略状态更新后失效缓存,确保下次读取最新数据。'''

    if kol_id is not None:

        _strategy_config_cache.pop((customer_id, kol_id), None)

async def _create_referral_commission(

    db: AsyncSession, customer_id: int, trade_id: int, pnl: float, symbol: str

) -> None:

    """根据下级客户平仓盈利,为其邀请人创建佣金记录。

    仅在 pnl > 0(正盈利)且该客户有邀请人(invited_by)时产生佣金,

    亏损不扣减(不做负佣金)。佣金记录在同一事务中提交(由调用方 commit)。

    """

    if pnl <= 0:

        return

    # 查找该客户的邀请人

    cust = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()

    if not cust or not cust.invited_by:

        return

    commission_amount = round(pnl * REFERRAL_COMMISSION_RATE, 6)

    commission = ReferralCommission(

        inviter_id=cust.invited_by,

        invitee_id=customer_id,

        trade_id=trade_id,

        invitee_pnl=pnl,

        commission_rate=REFERRAL_COMMISSION_RATE,

        commission_amount=commission_amount,

        symbol=symbol or "",

        note=f"自动结算: 下级平仓盈利 {pnl:.4f} * {REFERRAL_COMMISSION_RATE:.0%}",

    )

    db.add(commission)

    logger.info(

        f"邀请佣金记录: inviter_id={cust.invited_by} invitee_id={customer_id} "

        f"trade_id={trade_id} pnl={pnl:.4f} commission={commission_amount:.4f} symbol={symbol}"

    )

def _utcnow() -> datetime:

    return datetime.now(timezone.utc)

async def _get_active_master_position(

    db: AsyncSession,

    customer_id: int,

    exchange: str,

    symbol: str,

    side: str,

    kol_id: int | None = None,

    exchange_account_id: int | None = None,

    for_update: bool = False,

) -> Position | None:

    """查找指定客户/交易所/品种/方向的活跃主仓位(parent_id IS NULL)。

    kol_id 不为空时,仅查找同一 KOL 的主仓位；用于“同 KOL 同币种同方向”

    的重复新单拦截和补仓归属,避免不同 KOL 的同方向仓位互相拦截。

    """

    stmt = select(Position).where(

        Position.customer_id == customer_id,

        Position.exchange == exchange,

        Position.symbol == symbol,

        Position.side == side,

        Position.parent_id.is_(None),

        Position.status == "open",

    )

    if exchange_account_id is not None:

        stmt = stmt.where(Position.exchange_account_id == exchange_account_id)

    if for_update:

        stmt = stmt.with_for_update()

    if kol_id is not None:

        stmt = stmt.where(Position.kol_id == kol_id)

    return (await db.execute(stmt)).scalars().first()

def _is_add_position_signal(raw_text: str | None) -> bool:

    """识别补仓/加仓信号。

    补仓属于已有仓位的追加下单,不应被“已有持仓/冷却期”当作重复新单拒绝。

    """

    if not raw_text:

        return False

    keywords = (

        "补仓", "加仓", "追加仓位", "追加一笔", "追加一单", "再进一笔", "再进一单",

        "加一笔", "加一单", "加码", "补一笔", "补一单",

        "add position", "add-position", "add to position", "scale in", "increase position",

    )

    low = raw_text.lower()

    return any(keyword.lower() in low for keyword in keywords)

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

    # 容错:如果指定了 side 但找不到持仓,尝试不限方向查找该品种的所有持仓

    # (平仓信号 "ETH 平仓" 可能被误判方向,但用户意图是平掉 ETH 所有持仓)

    if not positions and parsed.symbol and parsed.side:

        logger.warning(

            f"平仓信号指定方向 {parsed.side} 无持仓,尝试不限方向查找 {parsed.symbol}: "

            f"customer={customer_id} kol_id={signal.kol_id}"

        )

        fallback_stmt = select(Position).where(

            Position.customer_id == customer_id,

            Position.exchange == exchange,

            Position.status == "open",

            Position.parent_id.is_not(None),

            Position.kol_id == signal.kol_id,

            Position.symbol == parsed.symbol,

        )

        positions = (await db.execute(fallback_stmt)).scalars().all()

        if positions:

            logger.info(f"容错找到 {len(positions)} 个 {parsed.symbol} 持仓(不限方向)")

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

            await db.rollback()

    if not closed_positions:

        await _log_signal_status(db, signal, "rejected", "平仓失败", customer_id)

        return {"ok": False, "reason": "平仓失败"}

    # 更新信号状态

    await _log_signal_status(

        db, signal, "ordered",

        f"平仓成功: {len(closed_positions)} 个持仓, 净盈亏 {total_pnl:.2f} USDT(已扣手续费)",

        customer_id

    )

    # 通知

    await notify(

        "tp_sl", "平仓成功",

        f"KOL {kol_name} 平仓信号\n品种: {parsed.symbol or '全部'}\n方向: {parsed.side or '全部'}\n平仓数: {len(closed_positions)}\n净盈亏: {total_pnl:.2f} USDT(已扣手续费)",

        customer_id,

        source_text=signal.raw_text,

    )

    return {

        "ok": True,

        "reason": f"已平仓 {len(closed_positions)} 个持仓",

        "position_ids": closed_positions,

        "total_pnl": total_pnl,

    }

async def _process_update_signal(

    db: AsyncSession,

    signal: Signal,

    parsed: ParsedSignal,

    customer_id: int,

    kol_name: str,

) -> dict:

    """处理止盈止损更新信号:更新已有持仓的 tp_levels 和/或 sl。

    逻辑:

    1. 查找该 KOL + 客户的未平仓子仓位(可选过滤 symbol)

    2. 若无 symbol 但 KOL 仅有一个 symbol 的持仓,自动推断;多 symbol 则拒绝

    3. 仅更新 parsed 中存在的字段:

       - 有 take_profits → 重建 tp_levels(基于现有 entry_price 和 side)

       - 有 stop_loss → 更新 sl 和 initial_sl

    4. 同时更新对应的 master 仓位,保持主子一致

    5. 通知 + 日志

    """

    # 校验:至少有 TP 或 SL 之一

    if not parsed.take_profits and parsed.stop_loss is None:

        await _log_signal_status(

            db, signal, "rejected",

            "更新信号无 TP 和 SL,无法更新", customer_id,

        )

        return {"ok": False, "reason": "更新信号无 TP 和 SL"}

    # 1. 获取交易所账号

    ex_acc = await _pick_exchange_account(db, customer_id)

    if not ex_acc:

        await _log_signal_status(db, signal, "rejected", "未配置交易所账号", customer_id)

        return {"ok": False, "reason": "未配置交易所账号"}

    exchange = ex_acc.exchange

    # 2. 查找该 KOL 的活跃子仓位

    stmt = select(Position).where(

        Position.customer_id == customer_id,

        Position.exchange == exchange,

        Position.status == "open",

        Position.parent_id.is_not(None),  # 只查子仓位

        Position.kol_id == signal.kol_id,

    )

    if parsed.symbol:

        stmt = stmt.where(Position.symbol == parsed.symbol)

    positions = (await db.execute(stmt)).scalars().all()

    # 3. 无 symbol 时尝试自动推断(若该 KOL 仅有一个 symbol 的持仓)

    if not positions and not parsed.symbol:

        all_kol_positions = (await db.execute(

            select(Position).where(

                Position.customer_id == customer_id,

                Position.exchange == exchange,

                Position.status == "open",

                Position.parent_id.is_not(None),

                Position.kol_id == signal.kol_id,

            )

        )).scalars().all()

        symbols = {p.symbol for p in all_kol_positions}

        if len(symbols) == 1:

            inferred_symbol = symbols.pop()

            positions = [p for p in all_kol_positions if p.symbol == inferred_symbol]

            logger.info(f"更新信号自动推断 symbol={inferred_symbol}(该 KOL 唯一持仓)")

        elif len(symbols) > 1:

            await _log_signal_status(

                db, signal, "rejected",

                f"更新信号未指定品种,且该 KOL 有 {len(symbols)} 个品种持仓,无法自动推断: {symbols}",

                customer_id,

            )

            return {"ok": False, "reason": "未指定品种且存在多个持仓品种(歧义)"}

    if not positions:

        logger.warning(

            f"更新信号未找到对应仓位: customer={customer_id} kol_id={signal.kol_id} "

            f"symbol={parsed.symbol}"

        )

        await _log_signal_status(

            db, signal, "rejected",

            f"无对应持仓可更新(该 KOL 无持仓或已平仓): {parsed.symbol or '全部'}",

            customer_id,

        )

        return {"ok": False, "reason": "无对应持仓"}

    # 4. 加载策略默认参数(用于 _build_tp_levels 的 close_pcts 配置)

    strategy, _ = await _get_cached_strategy_for_follow(db, customer_id, signal.kol_id)

    decision = strategy_engine.compute_decision(strategy)

    defaults = strategy_engine.get_strategy_defaults(decision.params or {})

    # 5. 按 symbol 分组,逐组更新(不同 symbol 的 entry_price/side 可能不同)

    from collections import defaultdict

    by_symbol: dict[str, list[Position]] = defaultdict(list)

    for pos in positions:

        by_symbol[pos.symbol].append(pos)

    updated_positions: list[int] = []

    updated_masters: set[int] = set()

    summary_parts: list[str] = []

    for sym, sym_positions in by_symbol.items():

        ref_pos = sym_positions[0]

        ref_side = ref_pos.side

        # 5.1 构建新的 tp_levels(若有 TP)

        new_tp_levels = None

        if parsed.take_profits:

            new_tp_levels = _build_tp_levels(parsed, defaults, ref_pos.entry_price, ref_side)

            for p in sym_positions:

                hit_levels = [t.get("level") for t in (p.tp_levels or []) if t.get("status") == "hit"]

                if hit_levels:

                    logger.warning(

                        f"更新信号覆盖已 hit 的 TP 级别: pos={p.id} hit_levels={hit_levels}"

                    )

        # 5.2 新的 sl(若有)

        new_sl = parsed.stop_loss if parsed.stop_loss is not None else None

        # 5.3 收集需要更新的 master IDs

        master_ids = {p.parent_id for p in sym_positions if p.parent_id is not None}

        masters_map: dict[int, Position] = {}

        if master_ids:

            masters = (await db.execute(

                select(Position).where(Position.id.in_(master_ids))

            )).scalars().all()

            masters_map = {m.id: m for m in masters if m.status == "open"}

        # 5.4 应用更新到所有子仓位

        for p in sym_positions:

            if new_tp_levels is not None:

                p.tp_levels = new_tp_levels

            if new_sl is not None:

                p.sl = new_sl

                # initial_sl 仅在尚未触发成本保护时同步更新

                if not p.breakeven_moved:

                    p.initial_sl = new_sl

            updated_positions.append(p.id)

        # 5.5 应用更新到对应 master 仓位

        for m in masters_map.values():

            if new_tp_levels is not None:

                m.tp_levels = new_tp_levels

            if new_sl is not None:

                m.sl = new_sl

                if not m.breakeven_moved:

                    m.initial_sl = new_sl

            updated_masters.add(m.id)

        summary_parts.append(

            f"{sym}({ref_side}): 子仓位 {len(sym_positions)} 个"

            + (f", TP→{parsed.take_profits}" if new_tp_levels else "")

            + (f", SL→{new_sl}" if new_sl is not None else "")

        )

    # 6. 提交事务

    try:

        await db.commit()

    except Exception as e:

        await db.rollback()

        logger.exception(f"更新信号提交失败: {e}")

        await _log_signal_status(db, signal, "rejected", f"更新失败: {e}", customer_id)

        return {"ok": False, "reason": f"更新失败: {e}"}

    # 7. 更新信号状态 + 事件推送 + 通知

    await _log_signal_status(

        db, signal, "ordered",

        f"止盈止损更新成功: {'; '.join(summary_parts)}",

        customer_id,

    )

    for pid in updated_positions:

        await bus.publish_customer(customer_id, "position", {

            "id": pid, "updated": True,

            "tp_levels": parsed.take_profits,

            "sl": parsed.stop_loss,

        })

    await notify(

        "tp_sl", "止盈止损已更新",

        f"KOL: {kol_name}\n品种: {', '.join(by_symbol.keys())}\n"

        f"更新内容: {'; '.join(summary_parts)}\n"

        f"涉及子仓位: {len(updated_positions)} 个, 主仓位: {len(updated_masters)} 个",

        customer_id,

        source_text=signal.raw_text,

    )

    return {

        "ok": True,

        "reason": f"已更新 {len(updated_positions)} 个子仓位, {len(updated_masters)} 个主仓位",

        "position_ids": updated_positions,

        "master_ids": list(updated_masters),

    }

def _normalize_symbol_for_exchange(ex, symbol: str) -> str:

    """将内部 symbol 格式归一化为交易所所需格式(与 exchange_adapter._normalize_symbol 一致)。

    OKX SWAP 需要 "BTC/USDT:USDT" 格式,内部使用 "BTC/USDT"。

    如果不归一化,ex.market("BTC/USDT") 返回 SPOT 市场(contractSize=None),

    导致合约大小转换失败,下单量缩小 100 倍。

    """

    if not symbol:

        return symbol

    if ":" in symbol:

        return symbol

    ex_name = getattr(ex, "id", "") or ""

    if ex_name.lower() == "okx" and "/USDT" in symbol:

        return f"{symbol}:USDT"

    return symbol

async def _notional_to_amount(ex, symbol: str, notional_usdt: float, price: float) -> float:

    """将 USDT 名义价值转为合约数量(按交易所精度取整)。

    重要: OKX 永续合约的 amount 是"合约数"而非币数。

    例如 ETH/USDT:USDT 的 contractSize=0.1,即 1 张合约 = 0.1 ETH。

    需要将 ETH 数量除以 contractSize 转换为合约数。

    # FIX: symbol 归一化 - 必须将 "BTC/USDT" 转为 "BTC/USDT:USDT" 后再查 market,

    # 否则 ex.market() 返回 SPOT 市场(contractSize=None),导致不除以 contractSize,

    # 下单量缩小 contractSize 倍(BTC 缩小 100 倍, ETH 缩小 10 倍)。

    """

    # FIX: symbol 归一化(与 exchange_adapter._normalize_symbol 逻辑一致)

    symbol = _normalize_symbol_for_exchange(ex, symbol)

    try:

        market = ex.market(symbol)

    except Exception:

        try:

            await ex.load_markets()

            market = ex.market(symbol)

        except Exception:

            market = None

    amount_eth = notional_usdt / price if price > 0 else 0

    # 合约大小转换: OKX 合约 amount 是合约数,需除以 contractSize

    contract_size = 1.0

    if market and market.get("contractSize"):

        try:

            contract_size = float(market["contractSize"])

        except (ValueError, TypeError):

            contract_size = 1.0

    if contract_size > 0 and contract_size != 1.0:

        amount_contracts = amount_eth / contract_size

    else:

        amount_contracts = amount_eth

    if hasattr(ex, "amount_to_precision"):

        try:

            s = ex.amount_to_precision(symbol, amount_contracts)

            return float(s)

        except (ValueError, TypeError):

            pass

    return amount_contracts

def _contracts_to_coin(ex, symbol: str, contracts: float) -> float:

    """将交易所返回的合约数转为实际币数。

    OKX 返回的 filled/contracts 是合约数,需乘以 contractSize 得到实际币数。

    """

    try:

        market = ex.market(symbol)

        cs = float(market.get("contractSize") or 1.0)

        if cs != 1.0:

            return contracts * cs

    except Exception:

        pass

    return contracts

async def _get_symbol_multiplier(db: AsyncSession, customer_id: int, symbol: str) -> float:

    """根据 symbol 查找倍率,优先级: 客户自定义币种 > 客户分类覆盖 > 管理员默认 > 1.0。

    优化: Redis 缓存(TTL 60s),减少高频信号下的数据库查询。

    """

    from app.models.symbol_config import SymbolNotionalConfig

    from app.models.customer_multiplier import CustomerSymbolMultiplier

    symbol_upper = symbol.upper()

    # Redis 缓存

    redis = None

    try:

        redis = await get_redis()

        cache_key = f"dcq:multiplier:{customer_id}:{symbol_upper}"

        cached = await redis.get(cache_key)

        if cached:

            return float(cached)

    except Exception:

        pass

    # 1. 客户自定义币种覆盖 (custom_symbol 不为空)

    custom_rows = (await db.execute(

        select(CustomerSymbolMultiplier).where(

            CustomerSymbolMultiplier.customer_id == customer_id,

            CustomerSymbolMultiplier.custom_symbol.isnot(None),

        )

    )).scalars().all()

    for cr in custom_rows:

        if symbol_upper.startswith(cr.custom_symbol.upper()):

            if redis:

                try: await redis.set(f"dcq:multiplier:{customer_id}:{symbol_upper}", str(cr.multiplier), ex=60)

                except Exception: pass

            return cr.multiplier

    # 2. 客户分类覆盖 (config_id 不为空, custom_symbol 为空)

    #    客户可以覆盖管理员预设分类的倍率,例如管理员设主流币=0.5,客户覆盖为1.0

    config_id_overrides = (await db.execute(

        select(CustomerSymbolMultiplier).where(

            CustomerSymbolMultiplier.customer_id == customer_id,

            CustomerSymbolMultiplier.config_id.isnot(None),

            CustomerSymbolMultiplier.custom_symbol.is_(None),

        )

    )).scalars().all()

    # 预加载这些 override 关联的 SymbolNotionalConfig

    if config_id_overrides:

        config_ids = [cr.config_id for cr in config_id_overrides]

        configs = (await db.execute(

            select(SymbolNotionalConfig).where(

                SymbolNotionalConfig.id.in_(config_ids),

                SymbolNotionalConfig.enabled.is_(True),

            )

        )).scalars().all()

        config_map = {c.id: c for c in configs}

        # 逐个检查:如果 symbol 匹配某个 config 的 symbols 列表,用客户的 multiplier

        for cr in config_id_overrides:

            cfg = config_map.get(cr.config_id)

            if not cfg:

                continue

            prefixes = cfg.symbol_list()

            if not prefixes:

                # 空 symbols = 兜底分类(其他),优先级最低,先记录不返回

                continue

            for prefix in prefixes:

                if symbol_upper.startswith(prefix):

                    if redis:

                        try: await redis.set(f"dcq:multiplier:{customer_id}:{symbol_upper}", str(cr.multiplier), ex=60)

                        except Exception: pass

                    return cr.multiplier

    # 3. 管理员全局默认配置

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

        if redis:

            try: await redis.set(f"dcq:multiplier:{customer_id}:{symbol_upper}", "1.0", ex=60)

            except Exception: pass

        return 1.0

    cm = (await db.execute(

        select(CustomerSymbolMultiplier).where(

            CustomerSymbolMultiplier.customer_id == customer_id,

            CustomerSymbolMultiplier.config_id == matched.id,

        )

    )).scalar_one_or_none()

    result = cm.multiplier if cm else matched.multiplier

    if redis:

        try: await redis.set(f"dcq:multiplier:{customer_id}:{symbol_upper}", str(result), ex=60)

        except Exception: pass

    return result

async def _process_cancel_order_signal(
    db: AsyncSession,
    signal: Signal,
    parsed: ParsedSignal,
    customer_id: int,
    kol_name: str,
) -> dict:
    """处理撤销未成交挂单信号。

    只撤销本系统 pending_orders 中尚未触发的服务端挂单。
    为避免误伤,当消息没有品种且没有方向时拒绝执行。
    """
    from app.models.pending_order import PendingOrder

    if not parsed.symbol and parsed.side not in ("long", "short"):
        reason = "撤挂单缺少品种或方向,为避免误伤未执行"
        logger.warning(f"撤挂单被拒: customer={customer_id} signal={signal.id} reason={reason}")
        await _log_signal_status(db, signal, "rejected", reason, customer_id)
        await notify(
            "error",
            "撤挂单已拒绝",
            f"KOL {kol_name}\n原因: {reason}",
            customer_id,
            source_text=signal.raw_text,
        )
        return {"ok": False, "reason": reason}

    stmt = select(PendingOrder).where(
        PendingOrder.customer_id == customer_id,
        PendingOrder.status == "pending",
    )
    if parsed.symbol:
        stmt = stmt.where(PendingOrder.symbol == parsed.symbol)
    if parsed.side in ("long", "short"):
        stmt = stmt.where(PendingOrder.side == parsed.side)

    pending_orders = (await db.execute(stmt)).scalars().all()
    target_prices = [float(p) for p in (parsed.entry_prices or []) if p]
    if not target_prices and parsed.entry_price:
        target_prices = [float(parsed.entry_price)]
    if target_prices:
        def _price_match(entry_price: float | None) -> bool:
            if entry_price is None:
                return False
            entry = float(entry_price)
            return any(abs(entry - target) / max(target, 1.0) <= 0.00001 for target in target_prices)

        pending_orders = [p for p in pending_orders if _price_match(p.entry_price)]

    if not pending_orders:
        reason = "没有匹配的待触发挂单"
        if target_prices:
            reason += f" (目标点位: {', '.join(str(p) for p in target_prices)})"
        logger.info(
            f"撤挂单无匹配: customer={customer_id} symbol={parsed.symbol} "
            f"side={parsed.side} target_prices={target_prices}"
        )
        await _log_signal_status(db, signal, "ordered", reason, customer_id)
        return {"ok": True, "cancelled": 0, "reason": reason}

    for pending in pending_orders:
        pending.status = "cancelled"
        pending.cancel_reason = parsed.reason or "KOL 撤挂单信号"

    await db.commit()
    ids = [p.id for p in pending_orders]
    reason = f"撤挂单完成: 已取消 {len(ids)} 个待触发挂单"
    logger.info(
        f"{reason}, customer={customer_id} symbol={parsed.symbol} "
        f"side={parsed.side} target_prices={target_prices} ids={ids}"
    )
    await _log_signal_status(db, signal, "ordered", reason, customer_id)
    await notify(
        "order",
        "待触发挂单已撤销",
        f"KOL: {kol_name}\n"
        f"品种: {parsed.symbol or '未指定'}\n"
        f"方向: {parsed.side or '未指定'}\n"
        f"目标点位: {', '.join(str(p) for p in target_prices) if target_prices else '未指定'}\n"
        f"取消数量: {len(ids)}\n"
        f"挂单ID: {', '.join(str(i) for i in ids)}",
        customer_id,
        source_text=signal.raw_text,
    )
    return {"ok": True, "cancelled": len(ids), "pending_ids": ids}

async def _refresh_matching_pending_orders(
    db: AsyncSession,
    *,
    parsed: ParsedSignal,
    customer_id: int,
) -> dict:
    """刷新挂单前先取消同品种/方向/点位的旧 pending。

    返回取消数量;调用方随后继续走正常挂单流程,让新 pending 使用新的过期时间。
    """
    from app.models.pending_order import PendingOrder

    if not parsed.symbol or parsed.side not in ("long", "short"):
        return {"cancelled": 0, "pending_ids": [], "reason": "缺少品种或方向"}

    target_prices = [float(p) for p in (parsed.entry_prices or []) if p]
    if not target_prices and parsed.entry_price:
        target_prices = [float(parsed.entry_price)]

    stmt = select(PendingOrder).where(
        PendingOrder.customer_id == customer_id,
        PendingOrder.status == "pending",
        PendingOrder.symbol == parsed.symbol,
        PendingOrder.side == parsed.side,
    )
    pending_orders = (await db.execute(stmt)).scalars().all()

    if target_prices:
        def _price_match(entry_price: float | None) -> bool:
            if entry_price is None:
                return False
            entry = float(entry_price)
            return any(abs(entry - target) / max(target, 1.0) <= 0.00001 for target in target_prices)

        pending_orders = [p for p in pending_orders if _price_match(p.entry_price)]

    if not pending_orders:
        logger.info(
            f"刷新挂单:未找到旧 pending,将按新信号创建: "
            f"customer={customer_id} symbol={parsed.symbol} side={parsed.side} prices={target_prices}"
        )
        return {"cancelled": 0, "pending_ids": [], "target_prices": target_prices}

    for pending in pending_orders:
        pending.status = "cancelled"
        pending.cancel_reason = "刷新挂单:旧 pending 已由新信号替换"

    await db.commit()
    ids = [p.id for p in pending_orders]
    logger.info(
        f"刷新挂单:已取消旧 pending,随后重新挂单: "
        f"customer={customer_id} symbol={parsed.symbol} side={parsed.side} "
        f"prices={target_prices} ids={ids}"
    )
    return {"cancelled": len(ids), "pending_ids": ids, "target_prices": target_prices}

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

    if _forced_exchange_account_id.get() is None:

        follow_accounts = await _list_follow_exchange_accounts(db, customer_id)

        if not follow_accounts:

            await _log_signal_status(db, signal, "rejected", "未配置可用跟单 API", customer_id)

            return {"ok": False, "reason": "未配置可用跟单 API"}

        if len(follow_accounts) > 1:

            results = []

            any_ok = False

            for acc in follow_accounts:

                token = _forced_exchange_account_id.set(acc.id)

                try:

                    one = await process_signal(db, signal, copy.deepcopy(parsed), customer_id)

                    one.update({
                        "exchange_account_id": acc.id,
                        "exchange": acc.exchange,
                        "account_label": acc.label,
                        "testnet": acc.testnet,
                    })

                    any_ok = any_ok or bool(one.get("ok"))

                    results.append(one)

                except Exception as e:

                    logger.exception(
                        f"多 API 跟单子账号失败: customer={customer_id} signal={signal.id} "
                        f"account={acc.id} {acc.exchange}"
                    )

                    results.append({
                        "ok": False,
                        "reason": str(e),
                        "exchange_account_id": acc.id,
                        "exchange": acc.exchange,
                        "account_label": acc.label,
                        "testnet": acc.testnet,
                    })

                finally:

                    _forced_exchange_account_id.reset(token)

            return {
                "ok": any_ok,
                "multi_api": True,
                "total": len(results),
                "success": sum(1 for r in results if r.get("ok")),
                "failed": sum(1 for r in results if not r.get("ok")),
                "results": results,
            }

    actions = getattr(parsed, "actions", None) or []
    has_cancel_order = "cancel_order" in actions
    has_refresh_pending = "refresh_pending" in actions
    has_open_action = any(a in ("open_long", "open_short") for a in actions)

    # 撤挂单动作先执行。
    # - 纯 "撤不挂了/撤单":撤完即结束。
    # - "撤单后重新挂多/开空":先撤旧挂单,再继续走后续开仓流程。
    if has_cancel_order:
        cancel_result = await _process_cancel_order_signal(db, signal, parsed, customer_id, kol_name)
        if not has_open_action:
            return cancel_result

    # "挂着" + 完整建仓参数:先刷新同参数旧 pending。
    # 没有旧 pending 时不返回,继续按当前信号新挂,避免漏单。
    if has_refresh_pending:
        await _refresh_matching_pending_orders(
            db,
            parsed=parsed,
            customer_id=customer_id,
        )

    # ---- 第4层过滤: 急停开关 ----

    # 客户 emergency_stop=True 时,拒绝所有新开仓信号(平仓信号不受影响)

    cust = (await db.execute(select(Customer).where(Customer.id == customer_id))).scalar_one_or_none()

    if cust:

        emergency_stop = getattr(cust, 'emergency_stop', False)

        if emergency_stop and not parsed.is_exit_signal:

            reason = "客户急停开关已开启,拒绝开仓信号"

            logger.warning(f"信号被拒(急停): customer={customer_id} signal={signal.id}")

            await _log_signal_status(db, signal, "rejected", reason, customer_id)

            await notify("risk", "信号已拒绝(急停)", f"KOL {kol_name}\n品种: {parsed.symbol}\n原因: {reason}", customer_id, source_text=signal.raw_text)

            return {"ok": False, "reason": reason}

    # 0. 检查是否为平仓信号

    if parsed.is_exit_signal:

        logger.info(f"处理平仓信号: customer={customer_id} symbol={parsed.symbol} side={parsed.side}")

        return await _process_exit_signal(db, signal, parsed, customer_id, kol_name)

    # 0.5 检查是否为止盈止损更新信号

    if parsed.is_update_signal:

        logger.info(

            f"处理止盈止损更新信号: customer={customer_id} symbol={parsed.symbol} "

            f"tp={parsed.take_profits} sl={parsed.stop_loss} reason={parsed.update_reason}"

        )

        return await _process_update_signal(db, signal, parsed, customer_id, kol_name)

    # 0.7 开仓信号前置校验:品种必须有效(避免 LLM 返回 UNKNOWN/空 或 URL 被误识别为 HTTPS/USDT)

    #     在调用交易所前拦截,避免下单报错或崩溃

    invalid_symbols = {"", "UNKNOWN/USDT", "UNKNOWN", "HTTPS/USDT", "HTTP/USDT"}

    if (not parsed.symbol) or (parsed.symbol.upper() in invalid_symbols):

        reason = f"无效品种: {parsed.symbol or '空'}"

        logger.warning(f"开仓信号被拒(无效品种): customer={customer_id} signal={signal.id} symbol='{parsed.symbol}'")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        # 非有效信号(无品种且无方向)不发告警通知,仅记录日志

        if not parsed.side:

            return {"ok": False, "reason": reason}

        await notify("error", "信号已拒绝", f"KOL {kol_name}\n品种无效: {parsed.symbol or '空'}\n原因: 无法识别交易品种", customer_id, source_text=signal.raw_text)

        return {"ok": False, "reason": reason}

    # 0.8 开仓信号前置校验:必须有方向和入场价(或可获取市价)

    #     缺失方向会导致交易所下单方向错误;两者都无价会导致 _place_entry 中 ValueError 崩溃

    if not parsed.side or parsed.side not in ("long", "short"):

        reason = f"无有效方向: {parsed.side or '空'}"

        logger.warning(f"开仓信号被拒(无方向): customer={customer_id} signal={signal.id}")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        await notify("error", "信号已拒绝", f"KOL {kol_name}\n品种: {parsed.symbol}\n原因: 未识别交易方向(多/空)", customer_id, source_text=signal.raw_text)

        return {"ok": False, "reason": reason}

    # 0.9 入场价缺失时,后续会用 market_price 兜底,此处不直接拒绝

    #     但若 entry_price 和 entry_prices 同时为 None,记录告警(仍允许走市价单)

    if parsed.entry_price is None and not parsed.entry_prices:

        logger.info(f"开仓信号无入场价,将使用市价兜底: customer={customer_id} signal={signal.id} symbol={parsed.symbol}")

    # 1. 策略与默认参数

    strategy, notional_override = await _get_cached_strategy_for_follow(db, customer_id, signal.kol_id)

    decision = strategy_engine.compute_decision(strategy)

    # 客户自定义跟单金额覆盖策略中的 base_qty

    if notional_override and notional_override > 0:

        decision.notional_usdt = notional_override

    # KOL 文本仓位比例覆盖本次下单金额:

    # 例如 "半仓"=50%, "三成仓"=30%, "轻仓"=30%, "重仓"=70%。

    # 只影响本次信号，不修改客户默认策略金额。

    position_pct = float(getattr(parsed, "position_pct", 0.0) or 0.0)

    if 0 < position_pct <= 100:

        original_notional = decision.notional_usdt

        decision.notional_usdt = round(decision.notional_usdt * position_pct / 100.0, 2)

        logger.info(

            f"信号仓位比例: symbol={parsed.symbol} position_pct={position_pct}% "

            f"notional={original_notional}->{decision.notional_usdt}"

        )

    # 1.1 应用品种分类倍率

    symbol_multiplier = await _get_symbol_multiplier(db, customer_id, parsed.symbol)

    if symbol_multiplier != 1.0:

        decision.notional_usdt = round(decision.notional_usdt * symbol_multiplier, 2)

        logger.info(f"品种分类倍率: symbol={parsed.symbol} multiplier={symbol_multiplier} notional={decision.notional_usdt}")

    defaults = strategy_engine.get_strategy_defaults(decision.params or {})

    # 统一止损配置:优先使用客户 RiskConfig.auto_stop_loss_pct。
    # 该值同时作为缺失 SL 的默认补充比例,以及 KOL 过宽 SL 的最大亏损上限。
    risk_cfg = await risk_manager.get_risk_config(db, customer_id, exchange)
    max_sl_pct = None
    if risk_cfg and risk_cfg.auto_stop_loss_pct and risk_cfg.auto_stop_loss_pct > 0:
        max_sl_pct = float(risk_cfg.auto_stop_loss_pct) / 100.0
        defaults["default_sl_pct"] = -max_sl_pct

    # 2. 交易所账号

    ex_acc = await _pick_exchange_account(db, customer_id)

    if not ex_acc:

        return {"ok": False, "reason": "未配置交易所账号"}

    if ex_acc.last_error:

        reason = (
            f"默认下单 API 验证失败,请在交易所账号页面测试成功后再跟单: "
            f"{ex_acc.exchange.upper()} {'测试网' if ex_acc.testnet else '实盘'} "
            f"{ex_acc.label or ex_acc.id}"
        )

        logger.warning(
            f"信号被拒(默认API验证失败): customer={customer_id} signal={signal.id} "
            f"exchange_account_id={ex_acc.id} error={ex_acc.last_error[:200]}"
        )

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        return {"ok": False, "reason": reason}

    exchange = ex_acc.exchange

    testnet = ex_acc.testnet

    exchange_account_id = ex_acc.id

    # 多 API 独立策略:单个 API 指定 strategy_id 时,优先使用 API 级策略覆盖 KOL 跟随策略。
    if getattr(ex_acc, "strategy_id", None):
        api_strategy = (
            await db.execute(
                select(Strategy).where(
                    Strategy.id == ex_acc.strategy_id,
                    Strategy.customer_id == customer_id,
                    Strategy.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if api_strategy:
            strategy = api_strategy
            notional_override = None
            decision = strategy_engine.compute_decision(strategy)
            defaults = strategy_engine.get_strategy_defaults(decision.params or {})
            logger.info(
                f"API级策略生效: customer={customer_id} account={exchange_account_id} "
                f"strategy={strategy.id} notional={decision.notional_usdt}"
            )
        else:
            logger.warning(
                f"API级策略不存在或停用,回退KOL策略: customer={customer_id} "
                f"account={exchange_account_id} strategy_id={ex_acc.strategy_id}"
            )

    # 多 API 独立倍率与单笔上限。
    follow_weight = float(getattr(ex_acc, "follow_weight", 1.0) or 1.0)
    if follow_weight != 1.0:
        before = decision.notional_usdt
        decision.notional_usdt = round(decision.notional_usdt * follow_weight, 2)
        logger.info(
            f"API级倍率: account={exchange_account_id} weight={follow_weight} "
            f"notional={before}->{decision.notional_usdt}"
        )
    account_max_order = float(getattr(ex_acc, "max_order_usdt", 0.0) or 0.0)
    if account_max_order > 0 and decision.notional_usdt > account_max_order:
        before = decision.notional_usdt
        decision.notional_usdt = account_max_order
        logger.info(
            f"API级单笔上限: account={exchange_account_id} "
            f"notional={before}->{decision.notional_usdt}"
        )

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

    # 5. KOL 连亏暂停检查

    kol_ok, kol_reason = await risk_manager.check_kol_can_trade(db, customer_id, signal.kol_id)

    if not kol_ok:

        await _log_signal_status(db, signal, "rejected", f"KOL 风控: {kol_reason}", customer_id)

        return {"ok": False, "reason": kol_reason}

    is_add_position = _is_add_position_signal(signal.raw_text)

    # 5.5 1小时冷却检查: 同KOL + 同币种 + 同方向 1小时内已开过仓 → 普通新单跳过

    # 补仓/加仓信号不走新单冷却拒绝,后续进入分批建仓。

    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    cooldown_reset_at = None
    try:
        _follow_for_cooldown = (
            await db.execute(
                select(KolFollow).where(
                    KolFollow.customer_id == customer_id,
                    KolFollow.kol_id == signal.kol_id,
                )
            )
        ).scalar_one_or_none()
        cooldown_reset_at = (
            getattr(_follow_for_cooldown, "cooldown_reset_at", None)
            if _follow_for_cooldown
            else None
        )
    except Exception:
        cooldown_reset_at = None
    effective_cooldown_since = one_hour_ago
    if cooldown_reset_at and cooldown_reset_at > effective_cooldown_since:
        effective_cooldown_since = cooldown_reset_at

    recent_pos = (

        await db.execute(

            select(Position).where(

                Position.customer_id == customer_id,

                Position.kol_id == signal.kol_id,

                Position.symbol == parsed.symbol,

                Position.side == parsed.side,

                Position.exchange_account_id == exchange_account_id,

                Position.opened_at >= effective_cooldown_since,

            ).limit(1)

        )

    ).scalar_one_or_none()

    if recent_pos and not is_add_position:

        reason = f"1小时冷却: 同KOL同币种同方向 {recent_pos.opened_at.strftime('%H:%M')} 已开仓"

        logger.info(f"信号被拒(冷却期): customer={customer_id} signal={signal.id} {reason}")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        return {"ok": False, "reason": reason}

    # ---- 并发锁: 防止同一客户+品种+方向的并发开仓 ----

    # 使用 PostgreSQL 事务级 advisory lock,事务结束自动释放

    # 防止两个并发信号同时通过"重复持仓检查"后各自创建仓位

    _lock_key = (int(__import__("hashlib").md5(f"{customer_id}|{exchange_account_id}|{parsed.symbol}|{parsed.side}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1

    try:

        _lock_acquired = (await db.execute(

            text("SELECT pg_try_advisory_xact_lock(:key)").bindparams(key=_lock_key)

        )).scalar()

        if not _lock_acquired:

            reason = "同一品种方向正在处理中,请稍后"

            logger.info(f"信号被并发锁阻止: cid={customer_id} {parsed.symbol} {parsed.side}")

            await _log_signal_status(db, signal, "rejected", reason, customer_id)

            # P1-2: 锁获取失败时抛出异常中止操作,不再静默返回

            raise RuntimeError(f"advisory lock 未获取: {reason}")

    except Exception as e:

        # P1-2: advisory lock 获取异常时抛出异常中止操作,不再降级为无锁继续

        reason = "系统繁忙，请稍后重试"

        logger.warning(f"advisory lock 获取异常,拒绝信号: cid={customer_id} {parsed.symbol} {parsed.side} err={e}")

        try:

            await _log_signal_status(db, signal, "rejected", reason, customer_id)

        except Exception:

            pass  # 数据库可能不可用,继续抛出原始异常

        raise

    # P1-2: 余额预检移到 advisory lock 之后(原在 lock 之前,可被并发绕过)

    try:

        from app.services.exchange_adapter import fetch_balance_fast

        bal_result = await fetch_balance_fast(
            exchange,
            db,
            customer_id,
            testnet,
            exchange_account_id=exchange_account_id,
        )

        leverage = max(float(parsed.leverage or 1), 1.0)
        required_margin = float(decision.notional_usdt or 0) / leverage
        available_margin = (
            bal_result.get("available_margin", 0)
            or bal_result.get("free", 0)
            or bal_result.get("balance", 0)
            or bal_result.get("equity", 0)
        )

        if available_margin > 0 and required_margin > available_margin * 0.99:

            reason = (
                f"余额不足: 下单名义价值{decision.notional_usdt} USDT / {leverage:g}x "
                f"需保证金{required_margin:.2f} USDT > 可用保证金{available_margin:.2f} USDT的99%"
            )

            logger.warning(f"信号被拒(余额不足): customer={customer_id} signal={signal.id} {reason}")

            await _log_signal_status(db, signal, "rejected", reason, customer_id)

            await notify("error", "信号已拒绝", f"KOL {kol_name}\n品种: {parsed.symbol}\n原因: {reason}", customer_id, source_text=signal.raw_text)

            return {"ok": False, "reason": reason}

    except Exception as e:

        logger.debug(f"余额预检跳过(非致命): {e}")

    # ---- 第4层过滤: 重复持仓检查 ----

    # 同 KOL + 同币种 + 同方向已有 open 持仓 -> 普通新单跳过

    # 补仓/加仓信号不拒绝,由 _place_entry 复用同 KOL 主仓进行分批建仓。

    existing_pos = await _get_active_master_position(

        db, customer_id, exchange, parsed.symbol, parsed.side,
        kol_id=signal.kol_id,
        exchange_account_id=exchange_account_id,
        for_update=True

    )

    if existing_pos and not is_add_position:

        reason = f"重复持仓: 同KOL同币种同方向已有持仓 pos={existing_pos.id}"

        logger.info(f"信号被拒(重复持仓): customer={customer_id} signal={signal.id} {reason}")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        return {"ok": False, "reason": reason}

    # ---- 第4层过滤: USDT 合约校验 ----

    # 只处理 USDT 永续合约,非 USDT 交易对跳过

    if parsed.symbol and "/USDT" not in parsed.symbol.upper():

        reason = f"非USDT合约: {parsed.symbol} (仅支持 USDT 永续)"

        logger.info(f"信号被拒(非USDT): customer={customer_id} signal={signal.id} {reason}")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        return {"ok": False, "reason": reason}

    # ---- 第4层过滤: KOL 频率限制(分发改拦截) ----

    # 5 分钟内同 KOL 超过 3 条开仓信号 -> 跳过

    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)

    recent_signal_count = (

        await db.execute(

            select(func.count(Signal.id)).where(

                Signal.kol_id == signal.kol_id,

                Signal.received_at >= five_min_ago,

                Signal.status.in_(["received", "ordered"]),

            )

        )

    ).scalar_one()

    if recent_signal_count > 3:

        reason = f"KOL频率限制: 5分钟内已发 {recent_signal_count} 条信号(上限3条)"

        logger.warning(f"信号被拒(KOL频率): customer={customer_id} signal={signal.id} {reason}")

        await _log_signal_status(db, signal, "rejected", reason, customer_id)

        return {"ok": False, "reason": reason}

    # 6. 风控(授权/静默/并发上限)——必须在写去重表之前检查

    can, reason = await check_can_trade(db, customer_id, exchange, parsed.symbol)

    if not can:

        await _log_signal_status(db, signal, "rejected", f"风控拒绝: {reason}", customer_id)

        return {"ok": False, "reason": reason}

    # 7. 获取市价用于纠错(优先读 Redis 缓存,减少交易所 API 调用)

    market_price = await _get_cached_market_price(exchange, parsed.symbol)

    if not market_price or market_price <= 0:

        market_price = await exchange_adapter.fetch_market_price(exchange, parsed.symbol)

    redis = await get_redis()

    # 8. 过滤与纠错(此处才写 Redis 去重表)

    fr = await signal_filter.filter_signal(

        parsed=parsed,

        redis=redis,

        market_price=market_price,

        default_tp_pct=defaults["default_tp_pct"],

        default_sl_pct=defaults["default_sl_pct"],

        no_stop_loss=defaults["no_stop_loss"],

        max_sl_pct=max_sl_pct,

        kol_id=signal.kol_id,

        dedup_scope=f"acct:{exchange_account_id}",

    )

    if not fr.accepted:

        # FilterResult.decision 可能是 "rejected"/"duplicate",但 Signal.status 只允许

        # rejected/ignored/ordered 等,需要映射: duplicate → ignored

        status = fr.decision if fr.decision in ("rejected", "ignored", "filtered") else "ignored"

        await _log_signal_status(db, signal, status, fr.reject_reason, customer_id, fr.dedup_hash)

        return {"ok": False, "reason": fr.reject_reason}

    # ---- 第5层过滤: 交易对存在性预校验 ----

    # 在调用交易所下单前,先验证交易对是否存在

    # 借鉴 KOL 跟单系统,避免下单时才报 BadSymbol 错误

    try:

        symbol_exists = await exchange_adapter.validate_symbol(exchange, parsed.symbol)

        if not symbol_exists:

            reason = f"交易对不存在: {exchange} 不支持 {parsed.symbol}"

            logger.warning(f"信号被拒(交易对不存在): customer={customer_id} signal={signal.id} {reason}")

            await _log_signal_status(db, signal, "rejected", reason, customer_id, fr.dedup_hash)

            await notify("error", "信号已拒绝", f"KOL {kol_name}\n品种: {parsed.symbol}\n原因: {reason}", customer_id, source_text=signal.raw_text)

            return {"ok": False, "reason": reason}

    except Exception as e:

        logger.debug(f"交易对校验跳过(非致命): {e}")

    # 9. 下单(智能分流:入场价远离市价时先创建待触发单)

    # ===== 分批建仓: KOL 给出多个入场价时, 均分下单量分别开仓 =====
    from app.services import pending_order_manager as _pom

    _entry_prices_batch = getattr(fr.signal, 'entry_prices', None) or []

    if len(_entry_prices_batch) > 1:

        _num_entries = len(_entry_prices_batch)

        _split_notional = round(decision.notional_usdt / _num_entries, 2)

        _original_ep = fr.signal.entry_price

        logger.info(

            f"分批建仓: {parsed.symbol} {_num_entries}个入场点, "

            f"每笔 {_split_notional} USDT, 总额 {decision.notional_usdt} USDT"

        )

        _batch_results = []

        for _i, _ep in enumerate(_entry_prices_batch):

            _batch_no = _i + 1

            fr.signal.entry_price = _ep

            _use_pending = (

                market_price

                and market_price > 0

                and _pom.should_use_pending_order(

                    _ep, market_price, fr.signal.side

                )

            )

            if _use_pending:

                try:

                    _po = await _pom.create_pending_order(

                        db,

                        customer_id=customer_id,

                        kol_id=signal.kol_id,

                        signal_id=signal.id,

                        exchange=exchange,

                        exchange_account_id=exchange_account_id,

                        parsed=fr.signal,

                        notional_usdt=_split_notional,

                        defaults=defaults,

                        strategy_id=strategy.id if strategy else None,

                        batch_no=_batch_no,

                    )

                    _ok = _po.get("ok", False)
                    _reason = _po.get("reason", "")

                    _batch_results.append({

                        "price": _ep, "batch_no": _batch_no,

                        "type": "pending", "ok": _ok,

                        "reason": _reason,

                    })

                    logger.info(

                        f"分批建仓 batch={_batch_no}/{_num_entries} "

                        f"入场价={_ep} → 待触发单 ok={_ok}"
                        + (f" reason={_reason}" if _reason else "")

                    )

                except Exception as _e:

                    logger.exception(

                        f"分批建仓 batch={_batch_no}/{_num_entries} 待触发单失败: {_e}"

                    )

                    _batch_results.append({

                        "price": _ep, "batch_no": _batch_no,

                        "type": "error", "ok": False, "error": str(_e),

                    })

            else:

                try:

                    _r = await _place_entry(

                        db,

                        customer_id=customer_id,

                        kol_id=signal.kol_id,

                        signal_id=signal.id,

                        exchange=exchange,

                        testnet=testnet,

                        exchange_account_id=exchange_account_id,

                        parsed=fr.signal,

                        notional_usdt=_split_notional,

                        defaults=defaults,

                        market_price=market_price,

                        strategy=strategy,

                    )

                    _ok = bool(_r and _r.get("order_id"))

                    _batch_results.append({

                        "price": _ep, "batch_no": _batch_no,

                        "type": "filled", "ok": _ok, "result": _r,

                    })

                    logger.info(

                        f"分批建仓 batch={_batch_no}/{_num_entries} "

                        f"入场价={_ep} → 已成交 ok={_ok}"

                    )

                except Exception as _e:

                    logger.exception(

                        f"分批建仓 batch={_batch_no}/{_num_entries} 下单失败: {_e}"

                    )

                    _batch_results.append({

                        "price": _ep, "batch_no": _batch_no,

                        "type": "error", "ok": False, "error": str(_e),

                    })

        # Restore original entry_price

        fr.signal.entry_price = _original_ep

        _filled = sum(1 for r in _batch_results if r.get("type") == "filled" and r.get("ok"))

        _pending = sum(1 for r in _batch_results if r.get("type") == "pending" and r.get("ok"))

        _failed = sum(1 for r in _batch_results if not r.get("ok"))

        await _log_signal_status(

            db, signal, "ordered",

            f"分批建仓: {_num_entries}个入场点, 成交{_filled}笔, 挂单{_pending}笔, 失败{_failed}笔",

            customer_id, fr.dedup_hash,

            corrected=fr.decision == "corrected",

        )

        # Publish order events to WebSocket

        for _r in _batch_results:

            if _r.get("ok") and _r.get("type") == "filled":

                _order_data = _r.get("result", {}).get("order")

                if _order_data:

                    await bus.publish_customer(customer_id, "order", _order_data)

        # Send notification

        _tp_str = "无"

        if parsed.take_profits:

            _tp_str = "\n".join(

                [f"  TP{i+1}: {p}" for i, p in enumerate(parsed.take_profits)]

            )

        _sl_str = f"{parsed.stop_loss}" if parsed.stop_loss else "无"

        _side_cn = (

            "做多(long)" if parsed.side == "long"

            else "做空(short)" if parsed.side == "short"

            else parsed.side

        )

        _entry_details = "\n".join([

            f"  点位{r['batch_no']}: {r['price']} → "

            + ("已成交" if r.get("type") == "filled" and r.get("ok")

               else "待触发" if r.get("type") == "pending" and r.get("ok")

               else "失败")

            for r in _batch_results

        ])

        await notify(

            "order",

            f"分批建仓 ({_num_entries}个点位)",

            f"KOL: {kol_name}\n"

            f"品种: {parsed.symbol}\n"

            f"方向: {_side_cn}\n"

            f"入场点位:\n{_entry_details}\n"

            f"止盈:\n{_tp_str}\n"

            f"止损: {_sl_str}\n"

            f"杠杆: {parsed.leverage}x\n"

            f"每笔金额: {_split_notional} USDT\n"

            f"总金额: {decision.notional_usdt} USDT",

            customer_id,

            source_text=signal.raw_text,

        )

        return {

            "ok": _filled > 0 or _pending > 0,

            "batch": True,

            "num_entries": _num_entries,

            "filled": _filled,

            "pending": _pending,

            "failed": _failed,

            "results": _batch_results,

        }

    # ===== 分批建仓结束 =====

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

            exchange_account_id=exchange_account_id,

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

            tp_str = "无"

            if parsed.take_profits:

                tp_str = "\n".join([f"  TP{i+1}: {p}" for i, p in enumerate(parsed.take_profits)])

            sl_str = f"{parsed.stop_loss}" if parsed.stop_loss else "无"

            side_cn = "做多(long)" if parsed.side == "long" else "做空(short)" if parsed.side == "short" else parsed.side

            await notify(

                "order", "信号已挂单等待",

                f"KOL: {kol_name}\n品种: {parsed.symbol}\n方向: {side_cn}\n"

                f"目标入场价: {fr.signal.entry_price}\n当前市价: {market_price}\n"

                f"止盈:\n{tp_str}\n止损: {sl_str}\n"

                f"杠杆: {parsed.leverage}x\n"

                f"等待价格触及后自动下单,7天内有效",

                customer_id,

                source_text=signal.raw_text,

            )

            return {"ok": True, "pending_id": po_result.get("pending_id"), "reason": "已创建待触发单"}

        else:

            await _log_signal_status(db, signal, "rejected", po_result.get("reason", ""), customer_id, fr.dedup_hash)

            return {"ok": False, "reason": po_result.get("reason", "创建待触发单失败")}

    # 8.5 市价单偏差校验(仅对市价单生效,待触发单已在上一步处理):

    # - 有利偏离: 多单当前价低于/等于报价,空单当前价高于/等于报价,放宽到 0.2%

    # - 不利偏离: 多单当前价高于报价,空单当前价低于报价,保持 0.1%

    if parsed.entry_price and market_price and market_price > 0:

        deviation = abs(parsed.entry_price - market_price) / market_price * 100

        is_favorable_price = (

            (parsed.side == "long" and market_price <= parsed.entry_price)

            or (parsed.side == "short" and market_price >= parsed.entry_price)

        )

        deviation_limit = 0.2 if is_favorable_price else 0.1

        deviation_type = "有利偏离" if is_favorable_price else "不利偏离"

        if deviation > deviation_limit:

            reason = (

                f"市价偏差过大({deviation_type}): KOL报价{parsed.entry_price} "

                f"vs 市价{market_price} 偏差{deviation:.2f}% > 阈值{deviation_limit:.2f}%"

            )

            logger.warning(f"信号被拒(市价偏差): customer={customer_id} signal={signal.id} {reason}")

            await _log_signal_status(db, signal, "rejected", reason, customer_id, fr.dedup_hash)

            await notify("error", "信号已拒绝", f"KOL {kol_name}\n品种: {parsed.symbol}\n原因: {reason}", customer_id, source_text=signal.raw_text)

            return {"ok": False, "reason": reason}

    # 入场价接近市价 → 直接市价下单

    try:

        result = await _place_entry(

            db,

            customer_id=customer_id,

            kol_id=signal.kol_id,

            signal_id=signal.id,

            exchange=exchange,

            testnet=testnet,

            exchange_account_id=exchange_account_id,

            parsed=fr.signal,

            notional_usdt=decision.notional_usdt,

            defaults=defaults,

            market_price=market_price,

            strategy=strategy,

        )

    except Exception as e:

        logger.exception(f"下单失败 customer={customer_id} signal={signal.id}")

        await _log_signal_status(db, signal, "rejected", f"下单异常: {e}", customer_id, fr.dedup_hash)

        await notify("error", "下单失败", f"客户{customer_id} {parsed.symbol} 下单异常: {e}", customer_id, source_text=signal.raw_text, kol_name=kol_name)

        return {"ok": False, "reason": f"下单异常: {e}"}

    # 10. 更新信号状态

    await _log_signal_status(db, signal, "ordered", fr.correct_log, customer_id, fr.dedup_hash, corrected=fr.decision == "corrected")

    if fr.correct_log:

        await notify("correct", "信号已纠错", f"KOL {kol_name} {parsed.symbol}\n{fr.correct_log}", customer_id, source_text=signal.raw_text)

    # 11. 事件推送

    if result.get("order"):

        await bus.publish_customer(customer_id, "order", result.get("order"))

    tp_str = "无"

    if parsed.take_profits:

        tp_str = "\n".join([f"  TP{i+1}: {p}" for i, p in enumerate(parsed.take_profits)])

    sl_str = f"{parsed.stop_loss}" if parsed.stop_loss else "无"

    side_cn = "做多(long)" if parsed.side == "long" else "做空(short)" if parsed.side == "short" else parsed.side

    await notify(

        "order", "跟单下单",

        f"KOL: {kol_name}\n品种: {parsed.symbol}\n方向: {side_cn}\n"

        f"入场: {parsed.entry_price}\n"

        f"止盈:\n{tp_str}\n止损: {sl_str}\n"

        f"杠杆: {parsed.leverage}x\n名义价值: {decision.notional_usdt} USDT",

        customer_id,

        source_text=signal.raw_text,

    )

    return {"ok": True, "order_id": result.get("order_id"), "position_id": result.get("position_id")}

# KOL 名称缓存(进程级,避免重复查询数据库)

# P3-1: 添加 60 秒 TTL,防止缓存数据过期不更新

_kol_name_cache: dict[int, tuple[str, float]] = {}

_KOL_NAME_CACHE_TTL = 60.0

async def _get_kol_name(db: AsyncSession, kol_id: int | None) -> str:

    """通过 kol_id 查询 KOL 名称,带进程级缓存(60s TTL)。"""

    if not kol_id:

        return ""

    import time

    now = time.time()

    cached = _kol_name_cache.get(kol_id)

    if cached:

        name, ts = cached

        if now - ts < _KOL_NAME_CACHE_TTL:

            return name

    try:

        kol = (await db.execute(select(Kol).where(Kol.id == kol_id))).scalar_one_or_none()

        name = kol.name if kol else "未知KOL"

        _kol_name_cache[kol_id] = (name, now)

        return name

    except Exception:

        return ""

async def _get_position_source_text(db: AsyncSession, position_id: int | None, kol_id: int | None = None, symbol: str = "") -> str:

    """通过持仓ID溯源原始 KOL 消息文本。

    查找路径: Position → Order(signal_id) → Signal(raw_text)

    如果找不到直接关联的信号,尝试通过 kol_id + symbol 查找最近匹配信号。

    """

    if not position_id:

        return ""

    try:

        # 1. 通过 position_id 找到关联的 Order(建仓单 tp_level=0)

        order = (await db.execute(

            select(Order).where(

                Order.position_id == position_id,

                Order.tp_level == 0,

            ).order_by(Order.id.desc()).limit(1)

        )).scalar_one_or_none()

        if order and order.signal_id:

            # 2. 通过 signal_id 获取原始消息

            sig = (await db.execute(

                select(Signal).where(Signal.id == order.signal_id)

            )).scalar_one_or_none()

            if sig and sig.raw_text:

                return sig.raw_text

        # 3. 兜底:通过 kol_id + symbol 查找最近的信号

        if kol_id and symbol:

            sig = (await db.execute(

                select(Signal).where(

                    Signal.kol_id == kol_id,

                    Signal.symbol == symbol,

                ).order_by(Signal.id.desc()).limit(1)

            )).scalar_one_or_none()

            if sig and sig.raw_text:

                return sig.raw_text

    except Exception as e:

        logger.debug(f"溯源持仓消息失败 pos={position_id}: {e}")

    return ""

async def _get_cached_market_price(exchange: str, symbol: str) -> float | None:

    """优先从 Redis 读取 position_manager 写入的价格缓存。"""

    try:

        redis = await get_redis()

        key = f"dcq:price:{exchange}:{symbol}"

        cached = await redis.get(key)

        if cached:

            price = float(cached)

            if price > 0:

                return price

    except Exception as e:

        logger.warning(f"读取市价缓存失败 {exchange}:{symbol}: {e}")

    return None

async def _pick_exchange_account(db: AsyncSession, customer_id: int):

    from app.models.config import ExchangeAccount

    forced_id = _forced_exchange_account_id.get()

    if forced_id is not None:
        acc = (await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.id == forced_id,
                ExchangeAccount.customer_id == customer_id,
                ExchangeAccount.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if acc:
            return acc

    stmt = select(ExchangeAccount).where(

        ExchangeAccount.customer_id == customer_id,

        ExchangeAccount.is_active.is_(True),

    ).order_by(
        ExchangeAccount.is_default.desc(),
        ExchangeAccount.last_error.asc(),
        ExchangeAccount.last_verified_at.desc().nullslast(),
        ExchangeAccount.id,
    )

    acc = (await db.execute(stmt)).scalars().first()
    if acc and acc.last_error:
        logger.warning(
            f"默认/候选交易所 API 最近验证失败: customer={customer_id} "
            f"exchange={acc.exchange} testnet={acc.testnet} id={acc.id} error={acc.last_error[:160]}"
        )
    return acc


async def _list_follow_exchange_accounts(db: AsyncSession, customer_id: int) -> list:

    """列出参与自动跟单的 API。

    第一版规则:
    - 显式开启 follow_enabled 的 API 全部参与跟单;
    - 若一个都没有开启,兼容旧逻辑:只使用默认/候选 API。
    - 最近验证失败(last_error 非空)的 API 不参与自动跟单。
    """

    from app.models.config import ExchangeAccount

    rows = (
        await db.execute(
            select(ExchangeAccount)
            .where(
                ExchangeAccount.customer_id == customer_id,
                ExchangeAccount.is_active.is_(True),
            )
            .order_by(
                ExchangeAccount.is_default.desc(),
                ExchangeAccount.last_error.asc(),
                ExchangeAccount.last_verified_at.desc().nullslast(),
                ExchangeAccount.id,
            )
        )
    ).scalars().all()

    enabled = [a for a in rows if a.follow_enabled and not a.last_error]
    if enabled:
        return enabled

    fallback = [a for a in rows if not a.last_error]
    return fallback[:1]

async def _verify_order_filled(ex, ex_order: dict, symbol: str) -> tuple[float, float]:

    """验证订单实际成交,返回 (filled_qty, filled_price)。

    防止"幽灵持仓"bug:OKX 返回 filled=None(订单未成交/被拒)时,

    旧代码用 amount 作为默认值创建了持仓记录,导致系统有持仓但交易所无。

    OKX 市价单是异步的:create_order 返回时 filled=None/status=None,但 sCode=0

    表示订单已被接受。需要等待并用 fetch_order 查询实际成交状态。

    重要:OKX testnet 在 long_short_mode 下,如果 create_order 因 posSide 缺失被拒,

    可能返回 sCode=0 + 假 ordId(实际订单不存在)。fetch_order 会返回

    "Order does not exist"(code=51603)。此时必须视为未成交,不能创建持仓。

    未成交时抛出 ValueError,由上层 process_signal 的 try/except 捕获并记录为 rejected。

    """

    import asyncio

    # 标准化 symbol 格式(OKX SWAP 需要 "BTC/USDT:USDT",与 place_order 一致)

    ex_name = getattr(ex, "id", "") or ""

    symbol = exchange_adapter._normalize_symbol(ex_name, symbol)

    filled = float(ex_order.get("filled") or 0)

    filled_price = float(ex_order.get("average") or ex_order.get("price") or 0)

    ex_status = ex_order.get("status")

    order_id = ex_order.get("id")

    info = ex_order.get("info", {}) or {}

    s_msg = info.get("sMsg", "")

    s_code = info.get("sCode", "")

    # 创建订单时如果 sCode != 0,直接拒绝(订单未被接受)

    if str(s_code) not in ("0", "") and not order_id:

        raise ValueError(

            f"订单被交易所拒绝(不创建持仓): sCode={s_code} sMsg={s_msg} order_id={order_id}"

        )

    # filled=0 但订单已被接受(sCode=0)→ 市价单异步,等待查询

    if filled <= 0 and order_id:

        logger.info(f"市价单异步处理中,等待查询成交: order_id={order_id} sMsg={s_msg}")

        fetch_failed_count = 0

        # 指数退避:1s,2s,4s,8s,最长 15s,最多 5 次查询

        delays = [1, 2, 4, 8]

        for attempt, delay in enumerate(delays):

            await asyncio.sleep(delay)

            try:

                fetched = await ex.fetch_order(order_id, symbol)

                filled = float(fetched.get("filled") or 0)

                filled_price = float(fetched.get("average") or fetched.get("price") or 0)

                f_status = fetched.get("status")

                logger.info(f"查询订单 {order_id} 第{attempt+1}次: status={f_status} filled={filled}")

                if filled > 0:

                    break  # 已成交

                if f_status in ("closed", "canceled", "rejected", "expired"):

                    break  # 终态,不再等待

            except Exception as e:

                # fetch_order 抛出异常(如 "Order does not exist")→ 订单不存在

                # 这通常意味着 create_order 返回了假 ordId(OKX testnet 的 bug),

                # 或者订单被异步取消。连续 2 次失败即视为未成交,不再等待。

                fetch_failed_count += 1

                logger.warning(

                    f"查询订单 {order_id} 失败(第{attempt+1}次,累计{fetch_failed_count}): {e}"

                )

                if fetch_failed_count >= 2:

                    raise ValueError(

                        f"订单查询连续失败(订单可能不存在,不创建持仓): "

                        f"order_id={order_id} error={e}"

                    )

    # 最终检查:仍未成交 → 拒绝(不创建幽灵持仓)

    if filled <= 0:

        raise ValueError(

            f"订单未成交(不创建持仓): status={ex_status} filled={filled} "

            f"sCode={s_code} sMsg={s_msg} order_id={order_id}"

        )

    # filled_price 缺失时尝试用 price 兜底

    if filled_price <= 0:

        filled_price = float(ex_order.get("price") or 0)

    # 将合约数转为实际币数(OKX contractSize 转换)

    filled = _contracts_to_coin(ex, symbol, filled)

    return filled, filled_price

async def _try_place_native_stop_loss(
    db: AsyncSession,
    ex,
    *,
    customer_id: int,
    kol_id: int,
    signal_id: int,
    exchange_account_id: int,
    exchange: str,
    position_id: int,
    symbol: str,
    side: str,
    qty: float,
    sl: float | None,
    leverage: int,
) -> None:
    """灰度提交交易所原生止损单,失败不影响系统内 1 秒止损兜底。"""
    if not settings.native_stop_loss_enabled or not sl or sl <= 0 or qty <= 0:
        return
    try:
        native_order = await exchange_adapter.place_native_stop_loss_order(
            ex, exchange, symbol, side, qty, sl
        )
        data = native_order.get("data") if isinstance(native_order, dict) else None
        first = data[0] if isinstance(data, list) and data else {}
        algo_id = (
            str(first.get("algoId") or first.get("ordId") or native_order.get("id") or "")
            if isinstance(native_order, dict) else ""
        )
        db.add(Order(
            customer_id=customer_id,
            kol_id=kol_id,
            signal_id=signal_id,
            position_id=position_id,
            exchange_account_id=exchange_account_id,
            exchange=exchange,
            symbol=symbol,
            side="sell" if side == "long" else "buy",
            type="stop_market",
            order_role="stop_loss",
            qty=qty,
            price=sl,
            leverage=leverage,
            status="pending",
            exchange_order_id=algo_id,
            created_at=_utcnow(),
        ))
        logger.info(f"原生止损单已提交 pos={position_id} symbol={symbol} sl={sl} algo_id={algo_id}")
    except Exception as e:
        logger.warning(
            f"原生止损单提交失败,保留系统内1秒止损兜底: "
            f"pos={position_id} symbol={symbol} sl={sl} err={e}"
        )


async def _place_entry(

    db: AsyncSession,

    *,

    customer_id: int,

    kol_id: int,

    signal_id: int,

    exchange: str,

    testnet: bool,

    exchange_account_id: int | None = None,

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

        candidate = await _get_active_master_position(

            db, customer_id, exchange, parsed.symbol, parsed.side,
            kol_id=kol_id,
            exchange_account_id=exchange_account_id,
            for_update=True

        )

        if candidate is not None:

            elapsed = (_utcnow() - candidate.opened_at).total_seconds() if candidate.opened_at else float("inf")

            if elapsed <= batch_window:

                master = candidate

                logger.info(f"分批建仓: 复用主仓 pos_id={candidate.id} elapsed={elapsed:.0f}s window={batch_window}s")

            else:

                logger.info(f"分批建仓窗口已过 ({elapsed:.0f}s > {batch_window}s), 创建新主仓")

    ex, acc = await exchange_adapter.load_exchange(
        db,
        customer_id,
        exchange,
        testnet,
        exchange_account_id=exchange_account_id,
    )
    exchange_account_id = acc.id

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

                ex, parsed.symbol, order_side, order_type, amount,

                leverage=parsed.leverage, position_side=parsed.side,

            )

            # 验证实际成交,防止幽灵持仓(未成交订单被误记录为持仓)

            filled_qty, filled_price = await _verify_order_filled(ex, ex_order, parsed.symbol)

            logger.info(

                f"下单成交: {parsed.symbol} {order_side} filled={filled_qty} price={filled_price} "

                f"ex_id={ex_order.get('id', '')}"

            )

            entry_fee = exchange_adapter.extract_fee_from_order(

                ex, ex_order, parsed.symbol, filled_qty, filled_price, order_type,

            )

            order = Order(

                customer_id=customer_id,

                kol_id=kol_id,

                signal_id=signal_id,

                exchange_account_id=exchange_account_id,

                exchange=exchange,

                symbol=parsed.symbol,

                side=order_side,

                type=order_type,

                order_role="entry",

                qty=amount,

                price=entry_price,

                leverage=parsed.leverage,

                batch_no=1,

                status="filled",

                exchange_order_id=str(ex_order.get("id", "")),

                filled_qty=filled_qty,

                filled_price=filled_price,

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

                exchange_account_id=exchange_account_id,

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

                entry_fee=entry_fee,

                opened_at=_utcnow(),

            )

            db.add(master_position)

            await db.flush()

            order.position_id = master_position.id

            sub_position = Position(

                customer_id=customer_id,

                kol_id=kol_id,

                exchange_account_id=exchange_account_id,

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

                entry_fee=entry_fee,

                opened_at=_utcnow(),

            )

            db.add(sub_position)

            # 确保子仓位 id 已生成，供原生止损订单记录正确关联。
            await db.flush()

            trade = Trade(

                customer_id=customer_id,

                kol_id=kol_id,

                position_id=master_position.id,

                order_id=order.id,

                exchange_account_id=exchange_account_id,

                exchange=exchange,

                symbol=parsed.symbol,

                side=order_side,

                qty=order.filled_qty,

                price=real_entry,

                fee=entry_fee,

                realized_pnl=0.0,

                is_close=False,

                tp_level=0,

                executed_at=_utcnow(),

            )

            db.add(trade)

            await _try_place_native_stop_loss(
                db,
                ex,
                customer_id=customer_id,
                kol_id=kol_id,
                signal_id=signal_id,
                exchange_account_id=exchange_account_id,
                exchange=exchange,
                position_id=sub_position.id,
                symbol=parsed.symbol,
                side=parsed.side,
                qty=order.filled_qty,
                sl=sl,
                leverage=parsed.leverage,
            )

            try:

                await db.commit()

            except Exception as e:

                await db.rollback()

                logger.error(f"数据库提交失败: {e}")

                raise

            return {

                "order_id": order.id,

                "position_id": sub_position.id,

                "order": _order_dict(order, kol_id),

            }

        else:

            # master 已存在,在交易所下单并创建子仓位(分批建仓)

            from sqlalchemy import func as sa_func, or_ as sa_or

            # P3-2: 排除已删除(deleted_at IS NOT NULL)和已取消(status='deleted')的订单

            stmt = select(sa_func.count()).select_from(Order).where(

                sa_or(

                    Order.position_id == master.id,

                    Order.position_id.in_(

                        select(Position.id).where(Position.parent_id == master.id)

                    ),

                ),

                Order.deleted_at.is_(None),

                Order.status.notin_(["cancelled", "deleted", "failed"]),

            )

            count = (await db.execute(stmt)).scalar() or 0

            next_batch_no = count + 1

            ex_order = await exchange_adapter.place_order(

                ex, parsed.symbol, order_side, "market", amount,

                leverage=parsed.leverage, position_side=parsed.side,

            )

            # 验证实际成交,防止幽灵持仓(分批建仓同样需要检查)

            filled_qty, filled_price = await _verify_order_filled(ex, ex_order, parsed.symbol)

            logger.info(

                f"分批建仓成交: {parsed.symbol} {order_side} batch={next_batch_no} "

                f"filled={filled_qty} price={filled_price} ex_id={ex_order.get('id', '')}"

            )

            entry_fee = exchange_adapter.extract_fee_from_order(

                ex, ex_order, parsed.symbol, filled_qty, filled_price, "market",

            )

            order = Order(

                customer_id=customer_id,

                kol_id=kol_id,

                signal_id=signal_id,

                exchange_account_id=exchange_account_id,

                exchange=exchange,

                symbol=parsed.symbol,

                side=order_side,

                type="market",

                order_role="entry",

                qty=amount,

                price=entry_price,

                leverage=parsed.leverage,

                batch_no=next_batch_no,

                status="filled",

                exchange_order_id=str(ex_order.get("id", "")),

                filled_qty=filled_qty,

                filled_price=filled_price,

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

                exchange_account_id=exchange_account_id,

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

                entry_fee=entry_fee,

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

                exchange_account_id=exchange_account_id,

                exchange=exchange,

                symbol=parsed.symbol,

                side=order_side,

                qty=order.filled_qty,

                price=sub_entry,

                fee=entry_fee,

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

            master.entry_fee = (master.entry_fee or 0) + entry_fee

            try:

                await db.commit()

            except Exception as e:

                await db.rollback()

                logger.error(f"数据库提交失败: {e}")

                raise

            return {

                "order_id": order.id,

                "position_id": sub_position.id,

                "order": _order_dict(order, kol_id),

            }

    finally:

        await exchange_adapter.close_exchange(ex)

def _build_tp_levels(parsed: ParsedSignal, defaults: dict, entry: float, side: str) -> list[dict]:

    """构建多级止盈配置 [{level, price, pct, status}]。

    支持两种 tp_levels 配置格式:

      简化格式(推荐): [10, 20, 30] -> 涨10%/20%/30%止盈,平仓比例自动均分

      旧格式(兼容): [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]] -> 涨幅+平仓比例

    """

    tps = parsed.take_profits or []

    # 读取配置,支持简化格式和旧格式

    raw_tp_cfg = defaults.get("tp_levels") or [3, 5, 8]

    if raw_tp_cfg and not isinstance(raw_tp_cfg[0], (list, tuple)):

        # === 简化格式: [10, 20, 30] ===

        n = len(raw_tp_cfg)

        # 平仓比例自动均分(如3级: 33%,33%,34%)

        close_pcts = [1.0 / n] * n

        close_pcts[-1] = 1.0 - sum(close_pcts[:-1])

        # 涨幅百分比: 整数(如10)转为小数(0.1)

        default_tp_pct = []

        for v in raw_tp_cfg:

            v = float(v)

            if v >= 1.0:

                v = v / 100.0  # 10 -> 0.1

            default_tp_pct.append(v)

    else:

        # === 旧格式兼容: [[0.10, 0.3], [0.20, 0.3], [0.30, 0.4]] ===

        tp_levels_cfg = raw_tp_cfg if raw_tp_cfg and isinstance(raw_tp_cfg[0], (list, tuple)) else [[0.03, 0.3], [0.05, 0.3], [0.08, 0.4]]

        close_pcts = []

        for x in tp_levels_cfg:

            v = float(x[1]) if len(x) >= 2 else 0.0

            if v > 1.0:

                v = v / 10.0

            close_pcts.append(v)

        default_tp_pct = []

        for x in tp_levels_cfg:

            v = float(x[0]) if len(x) >= 1 else 0.0

            if v > 1.0:

                v = v / 10.0 if v < 10.0 else v / 100.0

            default_tp_pct.append(v)

    levels = []

    # 1. 信号提供的止盈价:按位置从 close_pcts 取比例

    for i, tp in enumerate(tps):

        if i < len(close_pcts):

            pct = close_pcts[i]

        else:

            # KOL 提供的 TP 数量超过配置级数,额外 TP 使用均分比例

            pct = 1.0 / len(tps)

        levels.append({

            "level": i + 1,

            "price": float(tp),

            "pct": pct,

            "status": "pending",

        })

    # 2. 仅当 KOL 未提供任何止盈时,才使用默认涨幅级补全

    #    若 KOL 已给出止盈,则完全按照 KOL 的止盈进行平仓,不补充默认级别

    if not tps:

        for j in range(len(default_tp_pct)):

            if len(levels) >= 5:

                break

            pct_value = default_tp_pct[j]

            price = entry * (1 + pct_value) if side == "long" else entry * (1 - pct_value)

            if price <= 0:

                logger.warning(

                    f"_build_tp_levels: 跳过第{j+1}级止盈(价格<=0): "

                    f"entry={entry} side={side} pct={pct_value} price={price}"

                )

                continue

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

    try:

        await db.commit()

    except Exception as e:

        await db.rollback()

        logger.error(f"数据库提交失败: {e}")

        raise

def _order_dict(order: Order, kol_id: int | None) -> dict:

    return {

        "id": order.id,

        "customer_id": order.customer_id,

        "kol_id": kol_id,

        "exchange_account_id": order.exchange_account_id,

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

    """手动(或止盈止损触发)平仓,支持子仓位同步主仓位。

    注意:策略结果记录的幂等性保证:

    - 子仓位完全平仓时记录策略结果(record_trade_result)

    - 使用 with_for_update() 行级锁防止并发重复记录

    - 子仓位 status 变为 "closed" 后,任何后续 close_position 调用会在 L737 被拦截

    - close_at_tp_level 中完全平仓的子仓位也会设置 status="closed",后续不会重复记录

    """

    # 行级锁:防止并发平仓(手动+止盈止损同时触发)导致超卖/重复结算

    position = (await db.execute(

        select(Position).where(Position.id == position_id).with_for_update()

    )).scalar_one_or_none()

    if not position or position.status != "open":

        return {"ok": False, "reason": "持仓不存在或已平仓"}

    if qty is not None and qty <= 0:

        return {"ok": False, "reason": "平仓数量必须大于 0"}

    close_qty = qty if qty is not None else position.qty

    if close_qty <= 0:

        return {"ok": False, "reason": "平仓数量必须大于 0"}

    if close_qty > position.qty:

        close_qty = position.qty

    exchange_position = position

    master = None

    if position.parent_id is not None:

        # P0-2: 使用 for_update 行级锁防止并发平仓导致 master 数据不一致

        master = (await db.execute(

            select(Position).where(Position.id == position.parent_id).with_for_update()

        )).scalar_one_or_none()

        if master and master.status == "open":

            exchange_position = master

    ex, _ = await exchange_adapter.load_exchange(
        db,
        position.customer_id,
        position.exchange,
        exchange_account_id=position.exchange_account_id,
    )

    try:

        ex_order = await exchange_adapter.close_position_market(

            ex, exchange_position.symbol, exchange_position.side, close_qty

        )

        # 校验实际成交:复用 _verify_order_filled 处理 OKX 异步成交和假 ordId 问题

        # 防止"幽灵平仓"(系统记录已平仓但交易所实际未成交)

        filled, fill_price = await _verify_order_filled(ex, ex_order, exchange_position.symbol)

        # 提取平仓手续费(TAKER 费率)

        close_fee = exchange_adapter.extract_fee_from_order(

            ex, ex_order, position.symbol, filled, fill_price, "market",

        )

        # 计算开仓手续费分摊(按平仓数量占初始数量比例)

        if position.initial_qty and position.initial_qty > 0:

            opening_fee_portion = (position.entry_fee or 0) * (filled / position.initial_qty)

        else:

            opening_fee_portion = 0.0

        if position.side == "long":

            gross_pnl = (fill_price - position.entry_price) * filled

        else:

            gross_pnl = (position.entry_price - fill_price) * filled

        # 净盈亏 = 毛盈亏 - 开仓手续费分摊 - 平仓手续费

        pnl = gross_pnl - opening_fee_portion - close_fee

        order = Order(

            customer_id=position.customer_id,

            kol_id=position.kol_id,

            position_id=position.id,

            exchange_account_id=position.exchange_account_id,

            exchange=position.exchange,

            symbol=position.symbol,

            side="sell" if position.side == "long" else "buy",

            type="market",

            order_role="close",

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

        # 先 flush 拿到平仓订单 id,否则后续 Trade.order_id 会写成 NULL,

        # 导致利润统计/手续费统计按订单关联时漏掉平仓流水。

        await db.flush()

        trade = Trade(

            customer_id=position.customer_id,

            kol_id=position.kol_id,

            position_id=position.id,

            order_id=order.id,

            exchange_account_id=position.exchange_account_id,

            exchange=position.exchange,

            symbol=position.symbol,

            side=order.side,

            qty=filled,

            price=fill_price,

            fee=close_fee,

            realized_pnl=pnl,

            is_close=True,

            tp_level=-1,

            executed_at=_utcnow(),

        )

        db.add(trade)

        position.qty -= filled

        position.realized_pnl += pnl

        # 收集子仓位平仓记录(用于后续邀请佣金结算)

        child_close_records: list[tuple[Position, Trade, float]] = []

        if position.qty <= 0.0000001:

            position.qty = 0

            position.status = "closed"

            position.closed_at = _utcnow()

            # 仅子仓位自行平仓(止盈止损/手动)时记录策略结果;

            # master 直接平仓时由下方 children 循环对各子仓位分别记录,避免重复记账

            # 用 closed_at 检查是否已记录过,防止 close_at_tp_level 记录后再次触发

            try:

                if position.parent_id is not None and position.kol_id:

                    strat, _ = await _get_cached_strategy_for_follow(db, position.customer_id, position.kol_id)

                    if strat:

                        # 用 realized_pnl 判断胜负(包含之前分批止盈的 pnl),而非本次 pnl

                        # 例:TP1 盈利 200,止损亏损 100 → realized_pnl=100 > 0 → won=True

                        # notional 用入场价×初始数量,与策略 compute_decision 的 USDT 单位一致

                        notional = (position.entry_price or 0) * (position.initial_qty or 0)

                        await strategy_engine.record_trade_result(db, strat.id, won=position.realized_pnl > 0, notional_usdt=notional, break_even=abs(position.realized_pnl) < 0.01)

                        _invalidate_strategy_cache(position.customer_id, position.kol_id)

            except Exception as strat_e:

                logger.warning(f"策略结果记录失败(不影响平仓): {strat_e}")

        if master is not None and master.status == "open":

            master.qty -= filled

            master.realized_pnl += pnl

            if master.qty <= 0.0000001:

                master.qty = 0

                master.status = "closed"

                master.closed_at = _utcnow()

        elif position.parent_id is None and position.status == "closed":

            # 直接全部平仓 master 仓位时,用子仓位自己的 entry_price 计算 pnl

            # (与 close_at_tp_level 一致,不按 qty 比例分配 master pnl)

            # 因为各子仓位 entry_price 可能不同(分批建仓/马丁格尔),按比例分配会扭曲单个 KOL 的盈亏

            children = (

                await db.execute(

                    select(Position).where(

                        Position.parent_id == position.id,

                        Position.status == "open",

                    )

                )

            ).scalars().all()

            if children:

                # 有子仓位:master Trade 的 pnl 和 fee 置 0,由子仓位 Trade 用各自 entry_price 计算

                trade.realized_pnl = 0

                trade.fee = 0

                order_side_str = "sell" if position.side == "long" else "buy"

                # 按实际成交量比例分配各子仓位的平仓量

                total_child_qty = sum(c.qty for c in children)

                if total_child_qty > 0 and filled < total_child_qty:

                    close_ratio = filled / total_child_qty

                else:

                    close_ratio = 1.0

                for child in children:

                    child_qty = child.qty * close_ratio

                    # 用子仓位自己的 entry_price 计算 pnl(不用 master.entry_price 按比例分配)

                    if child.side == "long":

                        child_gross_pnl = (fill_price - child.entry_price) * child_qty

                    else:

                        child_gross_pnl = (child.entry_price - fill_price) * child_qty

                    # 子仓位开仓手续费分摊

                    if child.initial_qty and child.initial_qty > 0:

                        child_open_fee = (child.entry_fee or 0) * (child_qty / child.initial_qty)

                    else:

                        child_open_fee = 0.0

                    # 子仓位平仓手续费分摊(按数量比例分配总平仓手续费)

                    child_close_fee = close_fee * (child_qty / filled) if filled > 0 else 0.0

                    child_pnl = child_gross_pnl - child_open_fee - child_close_fee

                    child.qty -= child_qty

                    if child.qty <= 0.0000001:
                        child.qty = 0
                        child.status = "closed"
                        child.closed_at = _utcnow()

                    child.realized_pnl += child_pnl

                    # 子仓位完全平仓时,记录策略交易结果(用于马丁格尔胜率/熔断)

                    try:

                        if child.kol_id:

                            strat, _ = await _get_cached_strategy_for_follow(db, child.customer_id, child.kol_id)

                            if strat:

                                child_notional = (child.entry_price or 0) * (child.initial_qty or 0)

                                await strategy_engine.record_trade_result(db, strat.id, won=child.realized_pnl > 0, notional_usdt=child_notional, break_even=abs(child.realized_pnl) < 0.01)

                                _invalidate_strategy_cache(child.customer_id, child.kol_id)

                    except Exception as strat_e:

                        logger.warning(f"策略结果记录失败(不影响平仓): {strat_e}")

                    child_trade = Trade(

                        customer_id=child.customer_id,

                        kol_id=child.kol_id,

                        position_id=child.id,

                        order_id=order.id,

                        exchange_account_id=child.exchange_account_id,

                        exchange=child.exchange,

                        symbol=child.symbol,

                        side=order_side_str,

                        qty=child_qty,

                        price=fill_price,

                        fee=child_close_fee,

                        realized_pnl=child_pnl,

                        is_close=True,

                        tp_level=-1,

                        executed_at=_utcnow(),

                    )

                    db.add(child_trade)

                    child_close_records.append((child, child_trade, child_pnl))

            # 如果 children 为空(master 无子仓位),保留 master 的 Trade.realized_pnl=pnl,不处理

        # 注意:部分平仓 master(parent_id IS NULL 且 status 仍为 open)不处理子仓位,由调用方负责

        # 邀请佣金:仅正盈利时为邀请人结算(亏损不扣减,不做负佣金)

        # 先 flush 拿到 trade.id / child_trade.id,再创建佣金记录(与交易记录同一事务提交)

        try:

            await db.flush()

        except Exception as e:

            await db.rollback()

            logger.error(f"数据库 flush 失败: {e}")

            raise

        # 主仓位佣金:master-with-children 时 trade.realized_pnl 已置 0 → 自动跳过,

        # 由下方子仓位按各自 child_pnl 结算,避免重复/错记

        await _create_referral_commission(

            db, position.customer_id, trade.id, trade.realized_pnl, position.symbol

        )

        # 子仓位佣金(手动平 master 时,各子仓位按自身正盈利结算)

        for child, child_trade, child_pnl in child_close_records:

            await _create_referral_commission(

                db, child.customer_id, child_trade.id, child_pnl, child.symbol

            )

        try:

            await db.commit()

        except Exception as e:

            await db.rollback()

            logger.error(f"数据库提交失败: {e}")

            raise

        await bus.publish_customer(position.customer_id, "position", {"id": position.id, "status": position.status, "pnl": pnl})

        _pos_src = await _get_position_source_text(db, position.id, position.kol_id, position.symbol)

        _pos_kol_name = await _get_kol_name(db, position.kol_id)

        await notify(

            "tp_sl", "平仓成交",

            f"品种: {position.symbol}\n方向: {position.side}\n平仓价: {fill_price}\n数量: {filled}\n"

            f"毛盈亏: {gross_pnl:.2f} USDT\n开仓手续费: {opening_fee_portion:.4f} USDT\n平仓手续费: {close_fee:.4f} USDT\n"

            f"净盈亏: {pnl:.2f} USDT",

            position.customer_id,

            source_text=_pos_src,

            kol_name=_pos_kol_name,

        )

        return {"ok": True, "pnl": pnl, "gross_pnl": gross_pnl, "close_fee": close_fee, "opening_fee": opening_fee_portion, "status": position.status}

    except Exception as e:

        # P1-3: 平仓失败时记录详细原因,便于排查"幽灵平仓"和数据丢失

        # P1-13: 强制平仓(超时平仓)失败时,确保详细错误原因被记录

        logger.error(f"平仓失败 pos={position_id} qty={close_qty} symbol={position.symbol} side={position.side} customer={position.customer_id}: {e}", exc_info=True)
        try:
            if position.exchange_account_id:
                await exchange_adapter.invalidate_exchange_cache(position.exchange_account_id)
        except Exception:
            pass

        try:

            await notify(

                "error", "平仓失败",

                f"品种: {position.symbol}\n方向: {position.side}\n仓位ID: {position.id}\n平仓数量: {close_qty}\n失败原因: {e}",

                position.customer_id,

            )

        except Exception:

            pass

        await db.rollback()

        raise

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

            ex, _ = await exchange_adapter.load_exchange(
                db,
                customer_id,
                order.exchange,
                exchange_account_id=order.exchange_account_id,
            )

            try:

                await exchange_adapter.cancel_order(ex, order.exchange_order_id, order.symbol)

            finally:

                await exchange_adapter.close_exchange(ex)

        except Exception as e:

            logger.warning(f"交易所撤单失败: {e}")

    order.status = "deleted"

    order.deleted_at = _utcnow()

    try:

        await db.commit()

    except Exception as e:

        await db.rollback()

        logger.error(f"数据库提交失败: {e}")

        raise

    return {"ok": True}

async def apply_cost_protection(db: AsyncSession, position: Position) -> bool:

    """达到 TP1 或 +2% 利润后,止损上移至入场价+缓冲(成本保护)。返回是否更新。"""

    if position.breakeven_moved or position.status != "open":

        return False

    # 从策略参数读取成本保护缓冲,默认 0.02 (2%)

    buffer = 0.02

    if position.kol_id:

        follow = (await db.execute(

            select(KolFollow).where(

                KolFollow.customer_id == position.customer_id,

                KolFollow.kol_id == position.kol_id,

                KolFollow.enabled.is_(True),

            )

        )).scalar_one_or_none()

        if follow and follow.strategy_id:

            strat = (await db.execute(

                select(Strategy).where(Strategy.id == follow.strategy_id)

            )).scalar_one_or_none()

            if strat and strat.params:

                buffer = float(strat.params.get("cost_protection_buffer", 0.02))

    new_sl = position.entry_price * (1 + buffer) if position.side == "long" else position.entry_price * (1 - buffer)

    position.sl = new_sl

    position.cost_protection = True

    position.breakeven_moved = True

    try:

        await db.commit()

    except Exception as e:

        await db.rollback()

        logger.error(f"数据库提交失败: {e}")

        raise

    await bus.publish_customer(

        position.customer_id, "position",

        {"id": position.id, "cost_protection": True, "sl": new_sl, "msg": "成本保护已启用:止损上移至入场价"},

    )

    _cost_src = await _get_position_source_text(db, position.id, position.kol_id, position.symbol)

    _cost_kol_name = await _get_kol_name(db, position.kol_id)

    await notify(

        "tp_sl", "成本保护已启用",

        f"品种: {position.symbol}\n止损上移至入场价+缓冲: {new_sl}\n防止盈利单变亏损",

        position.customer_id,

        source_text=_cost_src,

        kol_name=_cost_kol_name,

    )

    return True

async def close_at_tp_level(db: AsyncSession, position: Position, level: int, price: float) -> dict:

    """达到某级止盈 → 按比例平仓 + 触发成本保护,支持子仓位聚合。"""

    if position.parent_id is not None:

        # P0-2: 使用 for_update 行级锁防止并发止盈平仓导致 master 数据不一致

        master = (await db.execute(

            select(Position).where(Position.id == position.parent_id).with_for_update()

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

        ex, _ = await exchange_adapter.load_exchange(
            db,
            master.customer_id,
            master.exchange,
            exchange_account_id=master.exchange_account_id,
        )

        try:

            ex_order = await exchange_adapter.close_position_market(

                ex, master.symbol, master.side, total_close_qty

            )

            # 校验实际成交:防止假 ordId 导致的幽灵平仓

            filled, fill_price = await _verify_order_filled(ex, ex_order, master.symbol)

            # 如果成交价缺失,用传入的当前价格兜底

            if fill_price <= 0:

                fill_price = float(price or 0)

            # 提取平仓手续费(TAKER 费率),按子仓位数量比例分配

            total_close_fee = exchange_adapter.extract_fee_from_order(

                ex, ex_order, master.symbol, filled, fill_price, "market",

            )

            if master.side == "long":

                total_pnl = (fill_price - master.entry_price) * filled

            else:

                total_pnl = (master.entry_price - fill_price) * filled

            result = {"ok": True, "pnl": total_pnl, "status": master.status}

            actual_total_pnl = 0.0  # 实际总盈亏(基于子仓位 entry_price 之和,扣除手续费)

            # 收集子仓位平仓记录(用于后续邀请佣金结算)

            sib_close_records: list[tuple[Position, Trade, float]] = []

            # 部分成交时按实际成交量比例缩放各子仓位平仓量

            if filled > 0 and filled < total_close_qty:

                scale = filled / total_close_qty

                for sib, target, close_qty in hit_siblings:

                    close_qty = close_qty * scale

                # 重新构建 hit_siblings

                hit_siblings = [(sib, target, close_qty * scale) for sib, target, close_qty in hit_siblings]

                total_close_qty = filled

            # 创建聚合平仓 Order 记录(一次平仓一个 Order,非每个子仓位一个)
            order_record = Order(
                customer_id=master.customer_id,
                kol_id=master.kol_id,
                signal_id=None,
                position_id=master.id,
                exchange_account_id=master.exchange_account_id,
                exchange=master.exchange,
                symbol=master.symbol,
                side="sell" if master.side == "long" else "buy",
                type="market",
                order_role="tp_close",
                qty=total_close_qty,
                price=fill_price,
                leverage=master.leverage,
                status="filled",
                exchange_order_id=str(ex_order.get("id", "")),
                filled_qty=filled,
                filled_price=fill_price,
                filled_at=_utcnow(),
            )
            db.add(order_record)
            await db.flush()

            for sib, target, close_qty in hit_siblings:

                # sib_pnl 基于子仓位自己的 entry_price 计算(不用 master.entry_price 按比例分配)

                # 因为各子仓位 entry_price 可能不同,按比例分配会扭曲单个 KOL 的盈亏

                if sib.side == "long":

                    sib_gross_pnl = (fill_price - sib.entry_price) * close_qty

                else:

                    sib_gross_pnl = (sib.entry_price - fill_price) * close_qty

                # 开仓手续费分摊(按平仓数量占初始数量比例)

                if sib.initial_qty and sib.initial_qty > 0:

                    sib_open_fee = (sib.entry_fee or 0) * (close_qty / sib.initial_qty)

                else:

                    sib_open_fee = 0.0

                # 平仓手续费分摊(按数量比例分配总平仓手续费)

                sib_close_fee = total_close_fee * (close_qty / filled) if filled > 0 else 0.0

                sib_pnl = sib_gross_pnl - sib_open_fee - sib_close_fee

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

                    try:

                        if sib.kol_id:

                            strat, _ = await _get_cached_strategy_for_follow(db, sib.customer_id, sib.kol_id)

                            if strat:

                                sib_notional = (sib.entry_price or 0) * (sib.initial_qty or 0)

                                await strategy_engine.record_trade_result(db, strat.id, won=sib.realized_pnl > 0, notional_usdt=sib_notional, break_even=abs(sib.realized_pnl) < 0.01)

                                _invalidate_strategy_cache(sib.customer_id, sib.kol_id)

                    except Exception as strat_e:

                        logger.warning(f"策略结果记录失败(不影响平仓): {strat_e}")

                order_side = "sell" if sib.side == "long" else "buy"

                trade = Trade(

                    customer_id=sib.customer_id,

                    kol_id=sib.kol_id,

                    position_id=sib.id,

                    order_id=order_record.id,

                    exchange_account_id=sib.exchange_account_id,

                    exchange=sib.exchange,

                    symbol=sib.symbol,

                    side=order_side,

                    qty=close_qty,

                    price=fill_price,

                    fee=sib_close_fee,

                    realized_pnl=sib_pnl,

                    is_close=True,

                    tp_level=level,

                    executed_at=_utcnow(),

                )

                db.add(trade)

                sib_close_records.append((sib, trade, sib_pnl))

            master.qty -= filled

            master.realized_pnl += actual_total_pnl  # 用实际子仓位 pnl 之和,确保 master.realized_pnl == sum(sub.realized_pnl)

            if master.qty <= 0.0000001:

                master.qty = 0

                master.status = "closed"

                master.closed_at = _utcnow()

            # 邀请佣金:仅正盈利时为邀请人结算(亏损不扣减,不做负佣金)

            # 先 flush 拿到 trade.id,再创建佣金记录(与交易记录同一事务提交)

            try:

                await db.flush()

            except Exception as e:

                await db.rollback()

                logger.error(f"数据库 flush 失败: {e}")

                raise

            for sib, sib_trade, sib_pnl in sib_close_records:

                await _create_referral_commission(

                    db, sib.customer_id, sib_trade.id, sib_pnl, sib.symbol

                )

            try:

                await db.commit()

            except Exception as e:

                await db.rollback()

                logger.error(f"数据库提交失败: {e}")

                raise

            if level == 1:

                for sib, _, _ in hit_siblings:

                    await apply_cost_protection(db, sib)

            await bus.publish_customer(master.customer_id, "position", {

                "id": position.id,

                "status": position.status,

                "pnl": actual_total_pnl,

            })

            _tp_agg_src = await _get_position_source_text(db, master.id, master.kol_id, master.symbol)

            _tp_agg_kol = await _get_kol_name(db, master.kol_id)

            await notify(

                "tp_sl", f"第{level}止盈达成(聚合)",

                f"品种: {master.symbol}\n方向: {master.side}\n平仓价: {fill_price}\n聚合数量: {filled}\n"

                f"平仓手续费: {total_close_fee:.4f} USDT\n净盈亏: {actual_total_pnl:.2f} USDT\n涉及子仓位: {len(hit_siblings)}",

                master.customer_id,

                source_text=_tp_agg_src,

                kol_name=_tp_agg_kol,

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

        # P0-1: 先标记 TP 状态再平仓,确保同一事务提交

        # 避免 close_position 成功但 TP 状态提交失败导致下次循环重复触发

        target["status"] = "hit"

        position.tp_levels = tp_levels

        await db.flush()

        result = await close_position(db, position.id, close_qty)

        if result.get("ok"):

            if level == 1:

                await apply_cost_protection(db, position)

            try:

                await db.commit()

            except Exception as e:

                await db.rollback()

                logger.error(f"数据库提交失败: {e}")

                raise

            _tp_src = await _get_position_source_text(db, position.id, position.kol_id, position.symbol)

            _tp_kol = await _get_kol_name(db, position.kol_id)

            await notify(

                "tp_sl", f"第{level}止盈达成",

                f"品种: {position.symbol}\n平仓比例: {target.get('pct')}\n平仓价: {price}\n盈亏: {result.get('pnl', 0):.2f}",

                position.customer_id,

                source_text=_tp_src,

                kol_name=_tp_kol,

            )

        return result

