"""信号解析单元测试。"""
from app.services.signal_parser import (
    apply_cancel_context_if_needed,
    apply_position_context_if_needed,
    detect_side,
    detect_recent_cancel_context,
    extract_position_context,
    extract_entry,
    extract_stop_loss,
    extract_symbol,
    extract_take_profits,
    normalize_symbol,
    parse_text,
)


def test_normalize_symbol_variants():
    assert normalize_symbol("$SOL") == "SOL/USDT"
    assert normalize_symbol("SOLUSDT") == "SOL/USDT"
    assert normalize_symbol("SOL/USDT") == "SOL/USDT"
    assert normalize_symbol("sol") == "SOL/USDT"
    assert normalize_symbol("USDT") == ""  # 稳定币过滤
    assert normalize_symbol("") == ""


def test_detect_side_chinese_english():
    assert detect_side("LONG $SOL") == "long"
    assert detect_side("short ETH") == "short"
    assert detect_side("做多 BTC") == "long"
    assert detect_side("做空 BTC") == "short"
    assert detect_side("buy now") == "long"
    assert detect_side("hello world") == ""


def test_extract_symbol_dollar():
    assert extract_symbol("$SOL long") == "SOL/USDT"
    assert extract_symbol("ETH/USDT buy") == "ETH/USDT"
    assert extract_symbol("BTCUSDT tp 100k") == "BTC/USDT"


def test_extract_entry_and_tp_sl():
    text = "$SOL long entry 150 TP 155 160 165 SL 145 lev 5x"
    entry, entries = extract_entry(text)
    assert entry == 150.0
    tps = extract_take_profits(text)
    assert tps == [155.0, 160.0, 165.0]
    assert extract_stop_loss(text) == 145.0


def test_extract_tp_levels_with_numbers():
    text = "$SOL long @ 150 TP1 155 TP2 160 TP3 165 SL 145"
    tps = extract_take_profits(text)
    assert tps == [155.0, 160.0, 165.0]


def test_parse_text_full():
    parsed = parse_text("$SOL long entry 150 TP 155 160 SL 145 lev 5x 仓位 10%")
    assert parsed.symbol == "SOL/USDT"
    assert parsed.side == "long"
    assert parsed.entry_price == 150.0
    assert parsed.take_profits == [155.0, 160.0]
    assert parsed.stop_loss == 145.0
    assert parsed.leverage == 1
    assert parsed.position_pct == 0.0
    assert parsed.confidence > 0.7


def test_parse_text_garbage():
    parsed = parse_text("今天天气不错")
    assert parsed.symbol == ""
    assert parsed.side == ""
    assert parsed.confidence == 0.0


def test_parse_text_missing_tp_sl():
    parsed = parse_text("$SOL long entry 150")
    assert parsed.symbol == "SOL/USDT"
    assert parsed.side == "long"
    assert parsed.entry_price == 150.0
    assert parsed.take_profits == []
    assert parsed.stop_loss is None


def test_single_cancel_message_triggers_cancel_context():
    parsed = parse_text("撤")
    assert parsed.action == "cancel_order"
    assert parsed.reason == "撤销未成交挂单"

    has_context, reason = detect_recent_cancel_context(["撤"])
    assert has_context is True
    assert "近期撤单消息" in reason


def test_cancel_context_turns_copied_strategy_into_cancel_order():
    current = """
BTC/USDT
做多
@ 64,800.0000
止盈 66,700.0000
止损 63,300.0000
"""
    parsed = parse_text(current)
    assert parsed.action == "open_long"
    assert parsed.entry_price == 64800.0

    guarded = apply_cancel_context_if_needed(current, parsed, ["撤，不挂了"])
    assert guarded.action == "cancel_order"
    assert guarded.actions == ["cancel_order"]
    assert guarded.symbol == "BTC/USDT"
    assert guarded.side == "long"
    assert guarded.entry_price == 64800.0
    assert guarded.stop_loss == 63300.0
    assert "旧挂单定位参数" in guarded.reason


def test_without_cancel_context_copied_strategy_still_open_order():
    current = "BTC/USDT 做多 @ 64800 止盈 66700 止损 63300"
    parsed = parse_text(current)
    guarded = apply_cancel_context_if_needed(current, parsed, [])
    assert guarded.action == "open_long"


def test_cancel_context_does_not_block_explicit_reopen():
    current = "重新挂 BTC/USDT 做多 @ 63800 止盈 65000 止损 63000"
    parsed = parse_text(current)
    guarded = apply_cancel_context_if_needed(current, parsed, ["撤，不挂了"])
    assert guarded.action == "open_long"
    assert guarded.entry_price == 63800.0


def test_position_context_message_is_not_open_order():
    text = "BTC 目前持有三个空单 118600 119200 119800"
    parsed = parse_text(text)
    assert parsed.action == ""
    assert parsed.confidence == 0.0
    assert "持仓" in parsed.reason

    ctx = extract_position_context(text)
    assert ctx is not None
    assert ctx.symbol == "BTC/USDT"
    assert ctx.side == "short"
    assert ctx.entry_prices == [118600.0, 119200.0, 119800.0]


def test_position_follow_up_uses_recent_holding_context():
    recent = ["BTC 目前持有三个空单 118600 119200 119800"]
    current = "没进的现在可以跟进"
    parsed = parse_text(current)
    completed = apply_position_context_if_needed(current, parsed, recent)

    assert completed.action == "open_short"
    assert completed.actions == ["open_short"]
    assert completed.symbol == "BTC/USDT"
    assert completed.side == "short"
    assert completed.entry_price == 118600.0
    assert completed.entry_prices == [118600.0, 119200.0, 119800.0]
    assert completed.confidence >= 0.75
    assert "持仓上下文跟进命中" in completed.reason


def test_position_follow_up_without_context_stays_ignored():
    current = "没进的现在可以跟进"
    parsed = parse_text(current)
    completed = apply_position_context_if_needed(current, parsed, [])

    assert completed.action == ""
    assert completed.symbol == ""
    assert completed.side == ""
    assert completed.confidence == 0.0
