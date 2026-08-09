import pytest
from fastapi import HTTPException

from app.api.trading import manual_order
from app.schemas.trading import ManualOrderRequest


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeExecuteResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return FakeScalarResult(self.value)


class FakeDb:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _stmt):
        return FakeExecuteResult(self.results.pop(0))


class CurrentUser:
    id = 7
    username = "customer"
    role = "customer"
    emergency_stop = False


class RiskConfigWithOnlyRealFields:
    enabled = True
    enable_trailing_stop = True
    trailing_callback_pct = 2.5
    max_position_usdt = 1000


class ExchangeAccount:
    id = 3
    exchange = "binance"
    is_active = True
    is_default = True
    last_error = ""
    auth_expires_at = None
    testnet = True


@pytest.mark.asyncio
async def test_manual_order_uses_strategy_defaults_not_missing_risk_fields(monkeypatch):
    captured = {}

    async def fake_place_entry(_db, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "order_id": 123}

    monkeypatch.setattr("app.api.trading.order_manager._place_entry", fake_place_entry)

    body = ManualOrderRequest(
        exchange="binance",
        symbol="BTC/USDT",
        side="buy",
        qty=100,
        price=50000,
        leverage=2,
        take_profits=[51000],
        stop_loss=49000,
    )
    result = await manual_order(body, CurrentUser(), FakeDb([RiskConfigWithOnlyRealFields(), ExchangeAccount()]))

    assert result["code"] == 0
    assert captured["defaults"]["enable_trailing"] is True
    assert captured["defaults"]["trailing_callback"] == 0.025
    assert captured["exchange_account_id"] == 3


@pytest.mark.asyncio
async def test_manual_order_rejects_max_position_usdt(monkeypatch):
    body = ManualOrderRequest(
        exchange="binance",
        symbol="BTC/USDT",
        side="buy",
        qty=1500,
        price=50000,
    )

    with pytest.raises(HTTPException) as exc:
        await manual_order(body, CurrentUser(), FakeDb([RiskConfigWithOnlyRealFields(), ExchangeAccount()]))

    assert exc.value.status_code == 400
    assert "下单金额超限" in exc.value.detail
