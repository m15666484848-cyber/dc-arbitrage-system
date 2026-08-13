"""信号过滤与自动纠错服务。

处理:
0. 意图识别(交易指令 vs 分析/复盘/假设)
1. 去重(dedup_hash + Redis 时间窗口,跨 KOL/同 KOL)
2. 全周期去重(防止 KOL 隔日复盘相同策略)
3. 价格纠错(入场价偏离市价过大 → 改市价/丢弃)
4. 方向纠错(long 但 TP<入场,或 SL>入场 → 翻转/丢弃)
5. 符号纠错(别名映射已在 parser 做)
6. 缺失止盈止损兜底(读策略默认 + 币种分层默认)
7. 黑名单(稳定币对/无符号无方向垃圾消息)
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field

from loguru import logger

from app.schemas.signal import ParsedSignal
from app.services.signal_parser import classify_signal_intent


@dataclass
class FilterResult:
    decision: str  # accept | reject | duplicate | corrected
    signal: ParsedSignal
    dedup_hash: str = ""
    correct_log: str = ""
    reject_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.decision in ("accept", "corrected")


DEDUP_WINDOW_SECONDS = 60  # 短期去重窗口
DEDUP_LONG_TERM_WINDOW_SECONDS = 7 * 24 * 3600  # 全周期去重窗口 (7天)

# ---------------------------------------------------------------------------
# 币种分类与分层止盈止损配置
# ---------------------------------------------------------------------------
# 主流币(BTC/ETH): 波动小, 止损收紧, 止盈梯度温和
# 中型币(SOL/BNB等): 波动中等
# 山寨币(其他): 波动大, 止损放宽, 止盈梯度更激进

# 主流币白名单 (fallback, 实际从数据库读取)
_MAJOR_COINS = frozenset({"BTC", "ETH"})
# 中型币白名单 (fallback, 实际从数据库读取)
_MIDCAP_COINS = frozenset({
    "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "LINK", "MATIC",
    "LTC", "BCH", "NEAR", "APT", "ARB", "OP", "FIL", "ICP",
    "INJ", "SUI", "SEI", "TIA", "STX", "RNDR", "GRT", "AAVE",
})

# 数据库驱动的币种分类缓存: { "BTC": "major", "SOL": "midcap", ... }
# 由 refresh_coin_tier_cache() 从 symbol_notional_configs 表加载
_COIN_TIER_CACHE: dict[str, str] = {}
# 分类名到 tier 的映射
_CATEGORY_TIER_MAP = {
    "主流币": "major",
    "中型币": "midcap",
    "山寨币": "altcoin",
}


async def refresh_coin_tier_cache(db=None) -> None:
    """从数据库 symbol_notional_configs 表刷新币种分类缓存。

    在以下时机调用:
    1. 应用启动时
    2. 品种分类增删改后
    """
    global _COIN_TIER_CACHE
    try:
        if db is None:
            from app.core.database import get_session_factory
            factory = get_session_factory()
            async with factory() as session:
                await _load_cache_from_db(session)
        else:
            await _load_cache_from_db(db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"刷新币种分类缓存失败,使用硬编码默认值: {e}")


async def _load_cache_from_db(db) -> None:
    """从数据库加载分类到缓存。"""
    global _COIN_TIER_CACHE
    from sqlalchemy import select
    from app.models.symbol_config import SymbolNotionalConfig

    configs = (await db.execute(
        select(SymbolNotionalConfig).where(
            SymbolNotionalConfig.enabled.is_(True)
        ).order_by(SymbolNotionalConfig.id)
    )).scalars().all()

    new_cache: dict[str, str] = {}
    for cfg in configs:
        tier = _CATEGORY_TIER_MAP.get(cfg.name, "altcoin")
        if cfg.symbols:
            for sym in cfg.symbols.split(","):
                sym = sym.strip().upper()
                if sym:
                    new_cache[sym] = tier
    _COIN_TIER_CACHE = new_cache
    import logging
    logging.getLogger(__name__).info(
        f"币种分类缓存已刷新: {len(new_cache)} 个币种 "
        f"(major={sum(1 for v in new_cache.values() if v=='major')}, "
        f"midcap={sum(1 for v in new_cache.values() if v=='midcap')}, "
        f"altcoin=兜底)"
    )

# 分层配置: sl 为负数(表示亏损方向), tp 为正数列表, hard_cap 为硬止损正数
TIERED_CONFIG: dict[str, dict] = {
    "major": {
        "sl": -0.03,           # 3% 止损
        "tp": [0.02, 0.04, 0.06],  # 2% / 4% / 6% 止盈
        "hard_cap": 0.08,      # 8% 硬止损上限
    },
    "midcap": {
        "sl": -0.05,           # 5% 止损
        "tp": [0.03, 0.06, 0.10],  # 3% / 6% / 10% 止盈
        "hard_cap": 0.12,      # 12% 硬止损上限
    },
    "altcoin": {
        "sl": -0.08,           # 8% 止损
        "tp": [0.05, 0.10, 0.15],  # 5% / 10% / 15% 止盈
        "hard_cap": 0.20,      # 20% 硬止损上限
    },
}


def classify_coin(symbol: str | None) -> str:
    """根据交易对符号判定币种类别: major / midcap / altcoin。

    优先从数据库分类缓存查找，缓存为空时回退到硬编码白名单。
    输入可以是 'BTCUSDT', 'BTC/USDT', 'BTC-USDT' 等格式。
    """
    if not symbol:
        return "altcoin"
    # 提取基础币种: 去掉引号、空格
    base = symbol.strip().upper().strip('"\'')
    # 先按交易对分隔符切割 (BTC/USDT → BTC)
    for sep in ("/", "-", "_"):
        if sep in base:
            base = base.split(sep)[0].strip()
            break
    # 再去掉报价货币后缀 (BTCUSDT → BTC)
    for quote in ("USDT", "USD", "BUSD", "TUSD", "USDC", "PERP"):
        if base.endswith(quote) and len(base) > len(quote):
            base = base[: -len(quote)]
            break
    base = base.strip()
    # 优先使用数据库缓存
    if _COIN_TIER_CACHE:
        return _COIN_TIER_CACHE.get(base, "altcoin")
    # 回退到硬编码白名单
    if base in _MAJOR_COINS:
        return "major"
    if base in _MIDCAP_COINS:
        return "midcap"
    return "altcoin"


def multiplier_to_tier(multiplier: float) -> str:
    """根据仓位倍率推断币种分层,用于自定义币种的止盈止损。

    倍率 >= 0.8 → major (主流币级别,止损收紧)
    倍率 >= 0.4 → midcap (中型币级别)
    倍率 < 0.4  → altcoin (山寨币级别,止损放宽)
    """
    if multiplier >= 0.8:
        return "major"
    elif multiplier >= 0.4:
        return "midcap"
    return "altcoin"


def get_tiered_config(symbol: str | None) -> dict:
    """获取币种对应的分层止盈止损配置。"""
    tier = classify_coin(symbol)
    return TIERED_CONFIG[tier]


# ---------------------------------------------------------------------------
# ATR (Average True Range) 动态止盈止损修正
# ---------------------------------------------------------------------------

def calculate_atr(klines: list[list], period: int = 14) -> float | None:
    """从K线数据计算ATR。

    klines 格式: [[timestamp, open, high, low, close, volume], ...]
    使用简单移动平均法。
    """
    if not klines or len(klines) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(klines)):
        try:
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i - 1][4])
        except (IndexError, ValueError, TypeError):
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    return atr if atr > 0 else None


async def get_atr_for_symbol(
    exchange: str, symbol: str, period: int = 14
) -> float | None:
    """获取指定币种的 ATR 值，带 Redis 缓存(5分钟TTL)。

    降级策略: K线获取失败时返回 None，由调用方降级到分层默认值。
    """
    from app.core.redis import get_redis
    from app.services import exchange_adapter

    # 尝试从 Redis 缓存读取
    try:
        redis = await get_redis()
        if redis:
            cache_key = f"dcq:atr:{exchange}:{symbol}:{period}"
            cached = await redis.get(cache_key)
            if cached:
                val = float(cached)
                if val > 0:
                    return val
    except Exception:
        pass

    # 获取 K 线数据
    try:
        klines = await exchange_adapter.fetch_ohlcv(exchange, symbol, "1h", period + 2)
    except Exception as e:
        logger.debug(f"获取K线失败 {exchange}:{symbol}: {e}")
        return None

    atr = calculate_atr(klines, period)

    # 写入 Redis 缓存
    if atr and atr > 0:
        try:
            redis = await get_redis()
            if redis:
                cache_key = f"dcq:atr:{exchange}:{symbol}:{period}"
                await redis.setex(cache_key, 300, str(atr))
        except Exception:
            pass

    return atr


def stop_loss_price_from_pct(side: str, ref_price: float, loss_pct: float) -> float:
    # 按方向和最大亏损比例计算止损价。loss_pct 使用正数,如 0.05。
    pct = abs(float(loss_pct or 0))
    if pct <= 0 or ref_price <= 0:
        return 0.0
    if side == "short":
        return round(ref_price * (1 + pct), 8)
    return round(ref_price * (1 - pct), 8)


def clamp_stop_loss_to_max_loss(
    parsed: ParsedSignal,
    ref_price: float | None,
    max_loss_pct: float | None,
) -> str:
    # 把 KOL 给出的过宽 SL 收紧到客户统一最大亏损比例内。
    if not parsed.stop_loss or not ref_price or ref_price <= 0:
        return ""
    if not max_loss_pct or max_loss_pct <= 0:
        return ""
    max_pct = abs(float(max_loss_pct))
    cap_sl = stop_loss_price_from_pct(parsed.side, ref_price, max_pct)
    old_sl = parsed.stop_loss
    if parsed.side == "long" and old_sl < cap_sl:
        parsed.stop_loss = cap_sl
    elif parsed.side == "short" and old_sl > cap_sl:
        parsed.stop_loss = cap_sl
    else:
        return ""
    return f"止损过宽,按最大亏损 {max_pct * 100:.2f}% 收紧: {old_sl}→{parsed.stop_loss}"


def compute_dedup_hash(symbol: str, side: str, entry_price: float | None, kol_id: int | None = None) -> str:
    """符号 + 方向 + 入场价分桶(±0.5%) + KOL ID。

    分桶公式: bucket = round(price / step), step = price * 0.005
    即: round(1 / 0.005) = 200,但价格不同时 step 不同,所以 bucket 也不同。
    正确实现:bucket = round(log(price) / log(1+0.005)),价格每变化0.5%bucket+1.
    简化实现:用价格的百分比桶,以0.5%为步长。
    kol_id: 加入去重指纹,防止不同 KOL 的相同信号被误判为重复。
    """
    if entry_price and entry_price > 0:
        # log_{1.005}(price):价格每增长0.5%,桶号+1
        bucket = str(int(round(math.log(entry_price) / math.log(1.005))))
    else:
        return ""  # 市价单不参与价格分桶去重
    raw = f"{kol_id or 0}|{symbol}|{side}|{bucket}"
    return hashlib.md5(raw.encode()).hexdigest()


def compute_full_strategy_hash(
    kol_id: int | None,
    symbol: str,
    side: str,
    entry_price: float | None,
    take_profits: list[float] | None,
) -> str:
    """
    全周期策略指纹: 用于防止 KOL 复盘时再次开单。
    规则: KOL + 品种 + 方向 + 入场价(分桶±1%) + 止盈列表(标准化)
    """
    bucket = ""
    if entry_price and entry_price > 0:
        # log_{1.01}(price):价格每增长1%,桶号+1
        bucket = str(int(round(math.log(entry_price) / math.log(1.01))))
    tp_str = ",".join(str(round(t, 2)) for t in (take_profits or []))
    raw = f"{kol_id}|{symbol}|{side}|{bucket}|{tp_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def is_duplicate(redis, dedup_hash: str, window: int = DEDUP_WINDOW_SECONDS) -> bool:
    """检查 Redis 是否已存在该 hash。"""
    if not redis or not dedup_hash:
        return False
    key = f"dedup:{dedup_hash}"
    # 只检查是否存在，不设置(SET 操作移到 set_dedup_keys)
    exists = await redis.get(key)
    return exists is not None


async def check_long_term_duplicate(
    redis, full_hash: str, kol_id: int | None, db=None
) -> tuple[bool, str]:
    """
    全周期去重检查:用 Redis 作为唯一真相源,7 天 TTL。

    设计决策:
      - 不查数据库(订单表未存 full_hash 字段)
      - Redis 重启会丢失长期去重数据,但可接受(概率低,且 60 秒短去重仍在)
      - 如需更强保证,可在 Position 表加 full_hash 字段并在此查询
    """
    if not full_hash or not redis:
        return False, ""

    key = f"dedup_long:{full_hash}"
    # 只检查是否存在，不设置(SET 操作移到 set_dedup_keys)
    exists = await redis.get(key)
    if exists is not None:
        return True, "长期去重:该策略在过去7天内已执行"

    return False, ""


async def set_dedup_keys(
    redis,
    dedup_hash: str = "",
    full_hash: str = "",
    short_window: int = DEDUP_WINDOW_SECONDS,
    long_window: int = DEDUP_LONG_TERM_WINDOW_SECONDS,
) -> None:
    """设置去重 key，在信号通过所有过滤后调用。"""
    if not redis:
        return
    if dedup_hash:
        await redis.set(f"dedup:{dedup_hash}", "1", ex=short_window, nx=True)
    if full_hash:
        await redis.set(f"dedup_long:{full_hash}", "1", ex=long_window, nx=True)


async def clear_dedup_keys(redis, dedup_hash: str = "", full_hash: str = ""):
    """清除去重key，在订单失败时调用"""
    if redis:
        if dedup_hash:
            await redis.delete(f"dedup:{dedup_hash}")
        if full_hash:
            await redis.delete(f"dedup_long:{full_hash}")


def correct_price(parsed: ParsedSignal, market_price: float | None) -> tuple[bool, str, bool]:
    """价格纠错:偏离市价过大判定笔误。返回 (是否修改, 说明, 是否拒绝)。"""
    if not parsed.entry_price or market_price is None or market_price <= 0:
        return False, "", False
    dev = abs(parsed.entry_price - market_price) / market_price
    if dev > 0.30:
        return False, f"入场价 {parsed.entry_price} 偏离市价 {market_price} 超过30%,疑似严重错误", True
    if dev > 0.15:
        old = parsed.entry_price
        ratio = market_price / old
        parsed.entry_price = market_price
        # 同步缩放分批建仓价格,保留分批信息(而非覆盖为单元素)
        if parsed.entry_prices:
            parsed.entry_prices = [ep * ratio for ep in parsed.entry_prices]
        else:
            parsed.entry_prices = [market_price]
        # 同步调整TP/SL,保持相对比例
        if parsed.take_profits:
            parsed.take_profits = [tp * ratio for tp in parsed.take_profits]
        if parsed.stop_loss:
            parsed.stop_loss *= ratio
        return True, f"入场价纠偏 {old} → {market_price} (偏离{dev:.1%}),TP/SL/分批价同步调整", False
    return False, "", False


def correct_direction(parsed: ParsedSignal) -> tuple[bool, str]:
    """方向纠错:long 但 TP<入场 或 SL>入场 → 翻转方向。"""
    if not parsed.side:
        return False, ""
    entry = parsed.entry_price
    if not entry:
        return False, ""
    if parsed.side == "long":
        if parsed.stop_loss and parsed.stop_loss > entry:
            new_side = "short"
            parsed.side = new_side
            # long→short: SL > entry 对于 short 是正确方向(止损在上方),无需翻转
            # 翻转方向时同步翻转止盈价(镜像到入场价另一侧),过滤无效价格
            if parsed.take_profits and parsed.entry_price:
                ep = parsed.entry_price
                mirrored_tps = [ep * 2 - tp for tp in parsed.take_profits]
                parsed.take_profits = [tp for tp in mirrored_tps if tp > 0]
            return True, f"long 止损 {parsed.stop_loss} > 入场 {entry},方向自动翻转为 short"
        wrong_tp = [tp for tp in parsed.take_profits if tp < entry]
        if wrong_tp and not any(tp > entry for tp in parsed.take_profits):
            new_side = "short"
            parsed.side = new_side
            # 翻转方向时同步翻转止盈价(镜像到入场价另一侧),过滤无效价格
            if parsed.take_profits and parsed.entry_price:
                ep = parsed.entry_price
                mirrored_tps = [ep * 2 - tp for tp in parsed.take_profits]
                parsed.take_profits = [tp for tp in mirrored_tps if tp > 0]
            return True, f"long 止盈全部低于入场 {entry},方向自动翻转为 short"
    if parsed.side == "short":
        if parsed.stop_loss and parsed.stop_loss < entry:
            new_side = "long"
            parsed.side = new_side
            # short→long: SL < entry 对于 long 是正确方向(止损在下方),无需翻转
            # 翻转方向时同步翻转止盈价(镜像到入场价另一侧),过滤无效价格
            if parsed.take_profits and parsed.entry_price:
                ep = parsed.entry_price
                mirrored_tps = [ep * 2 - tp for tp in parsed.take_profits]
                parsed.take_profits = [tp for tp in mirrored_tps if tp > 0]
            return True, f"short 止损 {parsed.stop_loss} < 入场 {entry},方向自动翻转为 long"
        wrong_tp = [tp for tp in parsed.take_profits if tp > entry]
        if wrong_tp and not any(tp < entry for tp in parsed.take_profits):
            new_side = "long"
            parsed.side = new_side
            # 翻转方向时同步翻转止盈价(镜像到入场价另一侧),过滤无效价格
            if parsed.take_profits and parsed.entry_price:
                ep = parsed.entry_price
                mirrored_tps = [ep * 2 - tp for tp in parsed.take_profits]
                parsed.take_profits = [tp for tp in mirrored_tps if tp > 0]
            return True, f"short 止盈全部高于入场 {entry},方向自动翻转为 long"
    return False, ""


def apply_defaults(
    parsed: ParsedSignal,
    market_price: float | None,
    default_tp_pct: list[float] | None = None,
    default_sl_pct: float | None = None,
    no_stop_loss: bool = False,
    max_sl_pct: float | None = None,
    symbol: str | None = None,
    atr_value: float | None = None,
    tier_hint: str | None = None,
) -> str:
    """缺失止盈止损兜底。返回说明。

    优先级:
      1. 策略配置显式传入的 default_tp_pct / default_sl_pct / max_sl_pct
      2. ATR 动态修正(如果提供了 atr_value)
      3. 币种分层默认值(TIERED_CONFIG)
      4. 无止损模式: 使用币种分层硬止损兜底
    """
    logs = []
    ref = parsed.entry_price or market_price
    if not ref or ref <= 0:
        return ""

    # 根据币种获取分层配置
    tier_cfg = get_tiered_config(symbol or parsed.symbol)
    tier_name = tier_hint or classify_coin(symbol or parsed.symbol)

    # 确定实际使用的止盈/止损/硬止损参数
    # 策略配置优先, 未配置则降级到分层默认值
    tp_pcts = default_tp_pct if default_tp_pct else tier_cfg["tp"]
    if default_sl_pct is not None:
        sl_pct = -abs(default_sl_pct)
    else:
        sl_pct = tier_cfg["sl"]
    hard_cap = max_sl_pct if (max_sl_pct and max_sl_pct > 0) else tier_cfg["hard_cap"]

    # ATR 动态修正: 如果有 ATR 值, 计算基于波动率的止损/止盈百分比
    atr_sl_pct: float | None = None
    atr_tp_pcts: list[float] | None = None
    if atr_value and atr_value > 0 and ref > 0:
        # ATR 止损: ATR × 1.5 / 价格 (取与分层止损更宽的那个, 避免被正常波动扫掉)
        atr_sl_pct = (atr_value * 1.5) / ref
        # ATR 止盈: 盈亏比 1:1, 1:2, 1:3
        atr_tp_pcts = [
            round((atr_value * 1.5) / ref, 6),
            round((atr_value * 3.0) / ref, 6),
            round((atr_value * 4.5) / ref, 6),
        ]

    # 1. 缺失止盈 → 按默认涨幅生成
    if not parsed.take_profits:
        # 策略未配置止盈 + 有ATR → 使用 ATR 止盈; 否则用分层默认
        if not default_tp_pct and atr_tp_pcts:
            tp_pcts_to_use = atr_tp_pcts
            tp_source = f"ATR动态(atr={atr_value:.6f})"
        else:
            tp_pcts_to_use = tp_pcts
            tp_source = f"{tier_name}分层默认"

        if tp_pcts_to_use:
            if parsed.side == "long":
                parsed.take_profits = [round(ref * (1 + p), 8) for p in tp_pcts_to_use]
            else:
                parsed.take_profits = [round(ref * (1 - p), 8) for p in tp_pcts_to_use]
            logs.append(
                f"缺失止盈,按{tp_source} {tp_pcts_to_use} 生成 {parsed.take_profits}"
            )

    # 2. 缺失止损 → 按默认比例生成(除非 no_stop_loss=True)
    if not parsed.stop_loss and not no_stop_loss:
        # 策略未配置止损 + 有ATR → 取 max(分层止损, ATR止损) 用更宽的
        if default_sl_pct is None and atr_sl_pct:
            tier_sl_abs = abs(tier_cfg["sl"])
            effective_sl_pct = max(tier_sl_abs, atr_sl_pct)
            sl_pct_to_use = -effective_sl_pct  # 保持负数格式
            sl_source = f"ATR动态(max(分层{tier_sl_abs:.3f},ATR{atr_sl_pct:.3f}))"
        else:
            sl_pct_to_use = sl_pct
            sl_source = f"{tier_name}分层默认"

        if sl_pct_to_use:
            if parsed.side == "long":
                parsed.stop_loss = round(ref * (1 + sl_pct_to_use), 8)
            else:
                parsed.stop_loss = round(ref * (1 - sl_pct_to_use), 8)
            logs.append(
                f"缺失止损,按{sl_source} {sl_pct_to_use} 生成 {parsed.stop_loss}"
            )

    # 3. 止损过宽 → 收紧到 hard_cap 内
    if parsed.stop_loss and not no_stop_loss:
        cap_log = clamp_stop_loss_to_max_loss(parsed, ref, hard_cap)
        if cap_log:
            logs.append(cap_log)

    # 4. no_stop_loss 模式 → 使用分层硬止损兜底
    if not parsed.stop_loss and no_stop_loss:
        hard_sl_pct = tier_cfg["hard_cap"]
        parsed.stop_loss = stop_loss_price_from_pct(parsed.side, ref, hard_sl_pct)
        logs.append(
            f"无止损模式:按{tier_name}分层硬止损 {hard_sl_pct * 100:.0f}% 兜底 {parsed.stop_loss}"
        )

    return "; ".join(logs)


async def filter_signal(
    parsed: ParsedSignal,
    redis,
    market_price: float | None,
    default_tp_pct: list[float] | None = None,
    default_sl_pct: float | None = None,
    no_stop_loss: bool = False,
    max_sl_pct: float | None = None,
    skip_duplicate: bool = True,
    kol_id: int | None = None,
    skip_intent_check: bool = False,
    dedup_scope: str = "",
    exchange: str | None = None,
    tier_hint: str | None = None,
) -> FilterResult:
    """对解析后的信号执行过滤与纠错。"""

    # 0. 平仓信号直接通过
    if parsed.is_exit_signal:
        # 平仓信号不需要纠错和去重
        return FilterResult(
            decision="accept",
            signal=parsed,
            correct_log=f"平仓信号: {parsed.exit_reason}",
        )

    # 0.5 止盈止损更新信号直接通过(跳过去重/纠错/默认值补充)
    if parsed.is_update_signal:
        return FilterResult(
            decision="accept",
            signal=parsed,
            correct_log=f"止盈止损更新信号: {parsed.update_reason}",
        )

    # 1. 意图识别 (如果未在前置步骤执行)
    #    图片信号经 LLM 高置信度解析后, parsed.raw_text 可能包含 LLM prompt 文本
    #    (如 "请分析这些图片..."), 其中的 "分析" 等词会误触发意图检测, 需跳过
    if not skip_intent_check and parsed.raw_text:
        if getattr(parsed, 'has_image', False) and parsed.confidence >= 0.8:
            logger.debug(f"图片信号已通过 LLM 高置信度验证(confidence={parsed.confidence}), 跳过意图检测")
        else:
            intent, reason = classify_signal_intent(parsed.raw_text)
            if intent == "noise":
                return FilterResult("reject", parsed, reject_reason=f"信号为噪音/公告: {reason}")
            if intent == "analysis":
                return FilterResult("reject", parsed, reject_reason=f"信号为分析/复盘/假设: {reason}")
            # 'trade' 和 'unknown' 继续执行后续流程

    # 黑名单:无符号或无方向 → 拒绝
    if not parsed.symbol:
        return FilterResult("reject", parsed, reject_reason="无交易符号")
    if not parsed.side:
        return FilterResult("reject", parsed, reject_reason="无方向")

    # 价格纠错
    logs = []
    price_changed, price_log, price_rejected = correct_price(parsed, market_price)
    if price_log:
        logs.append(price_log)
    if price_rejected:
        return FilterResult("reject", parsed, reject_reason=price_log)

    # 方向纠错
    dir_changed, dir_log = correct_direction(parsed)
    if dir_log:
        logs.append(dir_log)

    # 缺失止盈止损兜底 (使用币种分层默认值 + ATR动态修正)
    # 如果有交易所信息, 尝试获取 ATR 用于动态修正
    atr_value: float | None = None
    if exchange:
        try:
            atr_value = await get_atr_for_symbol(exchange, parsed.symbol)
        except Exception as e:
            logger.debug(f"获取ATR失败,降级到分层默认: {e}")

    default_log = apply_defaults(
        parsed,
        market_price,
        default_tp_pct,
        default_sl_pct,
        no_stop_loss,
        max_sl_pct,
        symbol=parsed.symbol,
        atr_value=atr_value,
        tier_hint=tier_hint,
    )
    if default_log:
        logs.append(default_log)

    # 1. 短期去重 (60秒窗口)
    dedup_hash = compute_dedup_hash(parsed.symbol, parsed.side, parsed.entry_price, kol_id)
    # SP-M2修复: 仅当 dedup_scope 和 dedup_hash 都非空时才加前缀,
    # 否则市价单(dedup_hash为空)会生成 "scope:" 这样的非空hash,导致同scope下所有市价单被误判为重复
    scoped_dedup_hash = f"{dedup_scope}:{dedup_hash}" if (dedup_scope and dedup_hash) else dedup_hash
    scoped_full_hash = ""
    if skip_duplicate and scoped_dedup_hash:
        try:
            if await is_duplicate(redis, scoped_dedup_hash):
                return FilterResult(
                    "duplicate", parsed, dedup_hash=scoped_dedup_hash, reject_reason="重复信号(短期去重)"
                )
        except Exception as _redis_err:
            logger.warning(f"Redis去重检查失败,降级跳过: {_redis_err}")

    # 2. 全周期去重 (7天窗口,防止 KOL 隔日复盘相同策略)
    if kol_id:
        full_hash = compute_full_strategy_hash(
            kol_id, parsed.symbol, parsed.side, parsed.entry_price, parsed.take_profits
        )
        scoped_full_hash = f"{dedup_scope}:{full_hash}" if dedup_scope else full_hash
        try:
            is_long_dup, dup_reason = await check_long_term_duplicate(redis, scoped_full_hash, kol_id)
            if is_long_dup:
                return FilterResult(
                    "duplicate",
                    parsed,
                    dedup_hash=scoped_dedup_hash,
                    reject_reason=f"长期去重:{dup_reason}",
                )
        except Exception as _redis_err:
            logger.warning(f"Redis长期去重检查失败,降级跳过: {_redis_err}")
        # 将 full_hash 存入 parsed 供后续下单流程使用
        parsed.dedup_full_hash = scoped_full_hash  # type: ignore

    # 3. 信号通过所有过滤后设置去重 key (避免拒绝/下单失败后仍占用窗口)
    if skip_duplicate:
        try:
            await set_dedup_keys(redis, scoped_dedup_hash, scoped_full_hash)
        except Exception as _redis_err:
            logger.warning(f"Redis去重key设置失败: {_redis_err}")

    decision = "corrected" if logs else "accept"
    return FilterResult(
        decision=decision,
        signal=parsed,
        dedup_hash=scoped_dedup_hash,
        correct_log=" | ".join(logs),
    )
