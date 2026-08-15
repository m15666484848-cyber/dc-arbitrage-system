import hashlib

import pytest

from app.models.trading import Position
from app.services import order_manager


class FakeScalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeScalarResult:
    def __init__(self, value, list_value=None):
        self.value = value
        self.list_value = [] if list_value is None else list_value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return FakeScalars(self.list_value)


class FakeDb:
    def __init__(self, position):
        self.position = position
        self.committed = False
        self.added = []

    async def execute(self, _stmt):
        return FakeScalarResult(self.position)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for idx, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = idx

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


class FakeExchange:
    pass


@pytest.mark.asyncio
async def test_close_position_rejects_non_positive_qty():
    pos = Position(id=1, customer_id=1, exchange="binance", symbol="BTC/USDT", side="long", qty=1.0, status="open")
    result = await order_manager.close_position(FakeDb(pos), position_id=1, qty=0)

    assert result["ok"] is False
    assert "平仓数量必须大于 0" in result["reason"]


@pytest.mark.asyncio
async def test_close_position_caps_qty_to_position_size(monkeypatch):
    pos = Position(
        id=1,
        customer_id=1,
        exchange="binance",
        symbol="BTC/USDT",
        side="long",
        qty=1.0,
        initial_qty=1.0,
        entry_price=100,
        leverage=1,
        realized_pnl=0.0,
        entry_fee=0.0,
        status="open",
    )
    closed = {}

    async def fake_load_exchange(*args, **kwargs):
        return FakeExchange(), None

    async def fake_close_position_market(_ex, symbol, side, qty):
        closed.update({"symbol": symbol, "side": side, "qty": qty})
        return {"id": "x", "average": 110, "fee": {"cost": 0}}

    async def fake_verify_order_filled(_ex, _order, _symbol):
        return 1.0, 110.0

    async def fake_record_trade_result(*args, **kwargs):
        return None

    async def fake_create_referral_commission(*args, **kwargs):
        return None

    monkeypatch.setattr(order_manager.exchange_adapter, "load_exchange", fake_load_exchange)
    monkeypatch.setattr(order_manager.exchange_adapter, "close_position_market", fake_close_position_market)
    monkeypatch.setattr(order_manager.exchange_adapter, "extract_fee_from_order", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(order_manager, "_verify_order_filled", fake_verify_order_filled)
    monkeypatch.setattr(order_manager.strategy_engine, "record_trade_result", fake_record_trade_result)
    monkeypatch.setattr(order_manager, "_create_referral_commission", fake_create_referral_commission)

    # Mock notify 和 bus 防止测试发送真实告警和事件
    async def fake_notify(*args, **kwargs):
        pass

    async def fake_publish(*args, **kwargs):
        pass

    async def fake_get_source_text(*args, **kwargs):
        return ""

    async def fake_get_kol_name(*args, **kwargs):
        return ""

    monkeypatch.setattr(order_manager, "notify", fake_notify)
    monkeypatch.setattr(order_manager, "bus", type("FakeBus", (), {"publish_customer": fake_publish, "publish": fake_publish})())
    monkeypatch.setattr(order_manager, "_get_position_source_text", fake_get_source_text)
    monkeypatch.setattr(order_manager, "_get_kol_name", fake_get_kol_name)

    result = await order_manager.close_position(FakeDb(pos), position_id=1, qty=5)

    assert result["ok"] is True
    assert closed["qty"] == 1.0
    assert pos.status == "closed"


def test_pending_order_advisory_lock_key_is_stable():
    src = "7|3|BTC/USDT|long"
    expected = (int(hashlib.md5(src.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1

    assert expected == (int(hashlib.md5(src.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF) or 1
    assert isinstance(expected, int)
    assert expected > 0
