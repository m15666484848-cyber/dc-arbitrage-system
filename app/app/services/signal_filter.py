"""信号过滤与自动纠错服务。

处理:
0. 意图识别(交易指令 vs 分析/复盘/假设)
1. 去重(dedup_hash + Redis 时间窗口,跨 KOL/同 KOL)
2. 全周期去重(防止 KOL 隔日复盘相同策略)
3. 价格纠错(入场价偏离市价过大 → 改市价/丢弃)
4. 方向纠错(long 但 TP<入场,或 SL>入场 → 翻转/丢弃)
5. 符号纠错(别名映射已在 parser 做)
6. 缺失止盈止损兜底(读策略默认)
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


def compute_dedup_hash(symbol: str, side: str, entry_price: float | None) -> str:
    """符号 + 方向 + 入场价分桶(±0.5%)。

    分桶公式: bucket = round(price / step), step = price * 0.005
    即: round(1 / 0.005) = 200,但价格不同时 step 不同,所以 bucket 也不同。
    正确实现:bucket = round(log(price) / log(1+0.005)),价格每变化0.5%bucket+1。
    简化实现:用价格的百分比桶,以0.5%为步长。
    """
    bucket = ""
    if entry_price and entry_price > 0:
        # log_{1.005}(price):价格每增长0.5%,桶号+1
        bucket = str(int(round(math.log(entry_price) / math.log(1.005))))
    raw = f"{symbol}|{side}|{bucket}"
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
    # SET NX:成功设置表示首次,失败表示已存在
    ok = await redis.set(key, "1", ex=window, nx=True)
    return ok is None  # None 表示 key 已存在


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
    ok = await redis.set(key, "1", ex=DEDUP_LONG_TERM_WINDOW_SECONDS, nx=True)
    if ok is None:
        return True, "长期去重:该策略在过去7天内已执行"

    return False, ""


def correct_price(parsed: ParsedSignal, market_price: float | None) -> tuple[bool, str]:
    """价格纠错:偏离市价过大判定笔误。返回 (是否修改, 说明)。"""
    if not parsed.entry_price or market_price is None or market_price <= 0:
        return False, ""
    dev = abs(parsed.entry_price - market_price) / market_price
    if dev > 0.30:
        return False, f"入场价 {parsed.entry_price} 偏离市价 {market_price} 超过30%,疑似严重错误"
    if dev > 0.15:
        old = parsed.entry_price
        parsed.entry_price = market_price
        parsed.entry_prices = [market_price]
        return True, f"入场价 {old} 偏离市价 {market_price} 超过15%,自动改为市价"
    return False, ""


def correct_direction(parsed: ParsedSignal) -> tuple[bool, str]:
    """方向纠错:long 但 TP<入场 或 SL>入场 → 翻转方向。"""
    if not parsed.side:
        return False, ""
    entry = parsed.entry_price
    if not entry:
        return False, ""
    if parsed.side == "long":
        if parsed.stop_loss and parsed.stop_loss > entry:
            parsed.side = "short"
            return True, f"long 止损 {parsed.stop_loss} > 入场 {entry},方向自动翻转为 short"
        wrong_tp = [tp for tp in parsed.take_profits if tp < entry]
        if wrong_tp and not any(tp > entry for tp in parsed.take_profits):
            parsed.side = "short"
            return True, f"long 止盈全部低于入场 {entry},方向自动翻转为 short"
    if parsed.side == "short":
        if parsed.stop_loss and parsed.stop_loss < entry:
            parsed.side = "long"
            return True, f"short 止损 {parsed.stop_loss} < 入场 {entry},方向自动翻转为 long"
        wrong_tp = [tp for tp in parsed.take_profits if tp > entry]
        if wrong_tp and not any(tp < entry for tp in parsed.take_profits):
            parsed.side = "long"
            return True, f"short 止盈全部高于入场 {entry},方向自动翻转为 long"
    return False, ""


def apply_defaults(
    parsed: ParsedSignal,
    market_price: float | None,
    default_tp_pct: list[float],
    default_sl_pct: float,
    no_stop_loss: bool,
) -> str:
    """缺失止盈止损兜底。返回说明。"""
    logs = []
    ref = parsed.entry_price or market_price
    if not ref or ref <= 0:
        return ""
    if not parsed.take_profits:
        if default_tp_pct:
            if parsed.side == "long":
                parsed.take_profits = [round(ref * (1 + p), 8) for p in default_tp_pct]
            else:
                parsed.take_profits = [round(ref * (1 - p), 8) for p in default_tp_pct]
            logs.append(f"缺失止盈,按默认 {default_tp_pct} 生成 {parsed.take_profits}")
    if not parsed.stop_loss and not no_stop_loss:
        if default_sl_pct:
            if parsed.side == "long":
                parsed.stop_loss = round(ref * (1 + default_sl_pct), 8)
            else:
                parsed.stop_loss = round(ref * (1 - default_sl_pct), 8)
            logs.append(f"缺失止损,按默认 {default_sl_pct} 生成 {parsed.stop_loss}")
    if not parsed.stop_loss and no_stop_loss:
        logs.append("无止损模式(高危),未设置止损")
    return "; ".join(logs)


async def filter_signal(
    parsed: ParsedSignal,
    redis,
    market_price: float | None,
    default_tp_pct: list[float] | None = None,
    default_sl_pct: float = -0.05,
    no_stop_loss: bool = False,
    skip_duplicate: bool = True,
    kol_id: int | None = None,
    skip_intent_check: bool = False,
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

    # 1. 意图识别 (如果未在前置步骤执行)
    if not skip_intent_check and parsed.raw_text:
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
    price_changed, price_log = correct_price(parsed, market_price)
    if price_log:
        logs.append(price_log)
        if "超过30%" in price_log:
            return FilterResult("reject", parsed, reject_reason=price_log)

    # 方向纠错
    dir_changed, dir_log = correct_direction(parsed)
    if dir_log:
        logs.append(dir_log)

    # 缺失止盈止损兜底
    default_log = apply_defaults(
        parsed,
        market_price,
        default_tp_pct or [0.10, 0.20],
        default_sl_pct,
        no_stop_loss,
    )
    if default_log:
        logs.append(default_log)

    # 1. 短期去重 (60秒窗口)
    dedup_hash = compute_dedup_hash(parsed.symbol, parsed.side, parsed.entry_price)
    if skip_duplicate and await is_duplicate(redis, dedup_hash):
        return FilterResult(
            "duplicate", parsed, dedup_hash=dedup_hash, reject_reason="重复信号(短期去重)"
        )

    # 2. 全周期去重 (7天窗口,防止 KOL 隔日复盘相同策略)
    if kol_id:
        full_hash = compute_full_strategy_hash(
            kol_id, parsed.symbol, parsed.side, parsed.entry_price, parsed.take_profits
        )
        is_long_dup, dup_reason = await check_long_term_duplicate(redis, full_hash, kol_id)
        if is_long_dup:
            return FilterResult(
                "duplicate",
                parsed,
                dedup_hash=dedup_hash,
                reject_reason=f"长期去重:{dup_reason}",
            )
        # 将 full_hash 存入 parsed 供后续下单流程使用
        parsed.dedup_full_hash = full_hash  # type: ignore

    decision = "corrected" if logs else "accept"
    return FilterResult(
        decision=decision,
        signal=parsed,
        dedup_hash=dedup_hash,
        correct_log=" | ".join(logs),
    )
