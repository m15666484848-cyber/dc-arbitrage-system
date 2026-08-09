"""信号解析单元测试。"""
from app.services.signal_parser import (
    detect_side,
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
    assert parsed.leverage == 5
    assert parsed.position_pct == 10.0
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
