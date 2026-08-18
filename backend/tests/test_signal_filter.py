"""信号过滤与纠错单元测试。"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.schemas.signal import ParsedSignal
from app.services.signal_filter import (
    apply_defaults,
    compute_dedup_hash,
    correct_direction,
    correct_price,
    filter_signal,
    try_acquire_dedup,
)
from app.services.risk_manager import is_in_silent_period


def _parsed(**kw):
    base = dict(symbol="SOL/USDT", side="long", entry_price=150.0,
                entry_prices=[150.0], take_profits=[155.0, 160.0], stop_loss=145.0,
                leverage=1, position_pct=0.0, raw_text="", confidence=0.8, has_image=False)
    base.update(kw)
    return ParsedSignal(**base)


# ---------- 价格纠错 ----------
def test_correct_price_within_tolerance():
    p = _parsed(entry_price=150.0, entry_prices=[150.0])
    changed, log, rejected = correct_price(p, market_price=152.0)
    assert not changed
    assert not rejected  # 偏离 1.3% < 15%


def test_correct_price_minor_typo_auto_fix():
    p = _parsed(entry_price=150.0, entry_prices=[150.0])
    # 偏离 20% > 15% → 自动改市价
    changed, log, rejected = correct_price(p, market_price=125.0)
    assert changed
    assert not rejected
    assert p.entry_price == 125.0
    assert "入场价纠偏" in log and "同步调整" in log


def test_correct_price_severe_rejected():
    p = _parsed(entry_price=150.0, entry_prices=[150.0])
    # 偏离 40% > 30% → 拒绝信号(由调用方判断)
    changed, log, rejected = correct_price(p, market_price=250.0)
    assert not changed
    assert rejected
    assert "超过30%" in log


# ---------- 方向纠错 ----------
def test_correct_direction_long_sl_above_entry():
    p = _parsed(side="long", entry_price=150.0, stop_loss=155.0)  # long 但止损>入场 → 翻转
    changed, log = correct_direction(p)
    assert changed
    assert p.side == "short"


def test_correct_direction_short_tp_below_entry():
    # short:合理 SL 应高于入场(155),TP 含一个低于入场(145)→ 不翻转
    p = _parsed(side="short", entry_price=150.0, stop_loss=155.0, take_profits=[145.0, 160.0])
    changed, _ = correct_direction(p)
    assert not changed


# ---------- 缺失止盈止损兜底 ----------
def test_apply_defaults_missing_tp_sl():
    p = _parsed(take_profits=[], stop_loss=None)
    log = apply_defaults(p, market_price=150.0, default_tp_pct=[0.10, 0.20],
                         default_sl_pct=-0.05, no_stop_loss=False)
    assert p.take_profits == [165.0, 180.0]  # long: 150*1.10, 150*1.20
    assert p.stop_loss == 142.5  # 150*0.95
    assert "缺失止盈" in log


def test_apply_defaults_no_stop_loss_mode():
    p = _parsed(take_profits=[], stop_loss=None)
    log = apply_defaults(p, market_price=150.0, default_tp_pct=[0.10],
                         default_sl_pct=-0.05, no_stop_loss=True)
    assert p.stop_loss == 132.0
    assert "硬止损" in log and "兜底" in log


# ---------- 去重 ----------
def test_compute_dedup_hash_stable():
    h1 = compute_dedup_hash("SOL/USDT", "long", 150.0)
    h2 = compute_dedup_hash("SOL/USDT", "long", 150.3)  # 0.2% 内同桶
    assert h1 == h2


def test_compute_dedup_hash_different_side():
    h1 = compute_dedup_hash("SOL/USDT", "long", 150.0)
    h2 = compute_dedup_hash("SOL/USDT", "short", 150.0)
    assert h1 != h2


@pytest.mark.asyncio
async def test_try_acquire_dedup_first_time_true():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value="OK")  # SET NX 成功 → 首次占用
    assert await try_acquire_dedup(redis, "hash1") is True


@pytest.mark.asyncio
async def test_try_acquire_dedup_second_time_false():
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=None)  # SET NX 失败 → 重复
    assert await try_acquire_dedup(redis, "hash1") is False


# ---------- 静默时段 ----------
def test_is_in_silent_period_cross_midnight():
    ranges = [{"start": "23:00", "end": "07:00"}]
    assert is_in_silent_period(ranges, datetime(2025, 12, 31, 18, 0, tzinfo=timezone.utc))  # 北京时间凌晨2点
    assert is_in_silent_period(ranges, datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc))  # 北京时间23:30
    assert not is_in_silent_period(ranges, datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc))  # 北京时间中午


def test_is_in_silent_period_normal_range():
    ranges = [{"start": "12:00", "end": "13:00"}]
    assert is_in_silent_period(ranges, datetime(2026, 1, 1, 4, 30, tzinfo=timezone.utc))
    assert not is_in_silent_period(ranges, datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc))


# ---------- 端到端过滤 ----------
@pytest.mark.asyncio
async def test_filter_signal_reject_no_symbol():
    p = ParsedSignal(symbol="", side="long")
    redis = AsyncMock()
    fr = await filter_signal(p, redis, market_price=150.0, skip_duplicate=False)
    assert fr.decision == "reject"
    assert "无交易符号" in fr.reject_reason


@pytest.mark.asyncio
async def test_filter_signal_accept_valid():
    p = _parsed()
    redis = AsyncMock()
    redis.set = AsyncMock(return_value="OK")
    fr = await filter_signal(p, redis, market_price=150.0, skip_duplicate=True)
    assert fr.accepted


@pytest.mark.asyncio
async def test_filter_signal_duplicate():
    p = _parsed()
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=None)  # 已存在
    fr = await filter_signal(p, redis, market_price=150.0, skip_duplicate=True)
    assert fr.decision == "duplicate"
