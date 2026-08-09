import pytest

from app.schemas.signal import ParsedSignal
from app.services import exchange_adapter, order_manager, position_manager, signal_filter
from app.models.trading import Position


class FakeDb:
    async def execute(self, *args, **kwargs):
        return None

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _balance_check_rejects(notional, leverage, available_margin):
    required_margin = float(notional) / max(float(leverage or 1), 1.0)
    return available_margin > 0 and required_margin > available_margin * 0.99


def test_margin_precheck_uses_leverage_not_notional():
    assert not _balance_check_rejects(1000, 10, 120)
    assert _balance_check_rejects(1000, 10, 90)


@pytest.mark.asyncio
async def test_update_trailing_stop_commits(monkeypatch):
    db = FakeDb()
    pos = Position(
        id=1,
        status="open",
        side="long",
        qty=1,
        entry_price=100,
        sl=90,
        trailing_stop=True,
        trailing_callback=0.1,
    )

    await position_manager._update_trailing_stop(db, pos, 120)

    assert round(pos.sl, 8) == 108
    assert getattr(db, "committed", False) is True


@pytest.mark.asyncio
async def test_stop_loss_loop_uses_batch_price_fetch(monkeypatch):
    calls = {"single": 0, "batch": 0, "closed": 0}
    positions = [
        Position(id=1, customer_id=1, exchange="binance", symbol="BTC/USDT", side="long", qty=1, sl=95, status="open", parent_id=10),
        Position(id=2, customer_id=1, exchange="binance", symbol="ETH/USDT", side="short", qty=1, sl=210, status="open", parent_id=10),
    ]

    class Scalars:
        def all(self):
            return positions

    class Result:
        def scalars(self):
            return Scalars()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args, **kwargs):
            return Result()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def fake_cached(*args, **kwargs):
        return None

    async def fake_set_cached(*args, **kwargs):
        return None

    async def fake_batch(exchange, symbols):
        calls["batch"] += 1
        assert exchange == "binance"
        assert set(symbols) == {"BTC/USDT", "ETH/USDT"}
        return {"BTC/USDT": 90, "ETH/USDT": 220}

    async def fake_single(*args, **kwargs):
        calls["single"] += 1
        return 1

    async def fake_add_lock(_id):
        return True

    async def fake_remove_lock(_id):
        return None

    async def fake_close(*args, **kwargs):
        calls["closed"] += 1
        return {"ok": True}

    sleeps = {"count": 0}

    async def fake_sleep(_seconds):
        sleeps["count"] += 1
        raise asyncio.CancelledError()

    import asyncio
    monkeypatch.setattr(position_manager, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(position_manager, "_get_cached_price", fake_cached)
    monkeypatch.setattr(position_manager, "_set_cached_price", fake_set_cached)
    monkeypatch.setattr(position_manager.price_feed, "fetch_market_prices", fake_batch)
    monkeypatch.setattr(position_manager.exchange_adapter, "fetch_market_prices_batch", fake_batch)
    monkeypatch.setattr(position_manager.exchange_adapter, "fetch_market_price", fake_single)
    monkeypatch.setattr(position_manager, "_add_closing_position", fake_add_lock)
    monkeypatch.setattr(position_manager, "_remove_closing_position", fake_remove_lock)
    monkeypatch.setattr(position_manager.order_manager, "close_position", fake_close)
    monkeypatch.setattr(position_manager.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await position_manager.stop_loss_monitor_loop()

    assert calls["batch"] == 1
    assert calls["single"] == 0
    assert calls["closed"] == 2



def test_stop_loss_clamp_too_wide_long():
    p = ParsedSignal(symbol="BTC/USDT", side="long", entry_price=100.0, stop_loss=80.0)
    log = signal_filter.clamp_stop_loss_to_max_loss(p, 100.0, 0.05)
    assert p.stop_loss == 95.0
    assert "止损过宽" in log


def test_stop_loss_clamp_too_wide_short():
    p = ParsedSignal(symbol="BTC/USDT", side="short", entry_price=100.0, stop_loss=130.0)
    log = signal_filter.clamp_stop_loss_to_max_loss(p, 100.0, 0.05)
    assert p.stop_loss == 105.0
    assert "止损过宽" in log


def test_timeout_profitable_with_sl_should_alert_only_condition():
    pos = Position(id=1, symbol="BTC/USDT", side="long", qty=1, entry_price=100, sl=105, status="open")
    pnl, _ = position_manager.compute_pnl(pos, 120)
    assert pnl > 0
    assert pos.sl > 0



@pytest.mark.asyncio
async def test_partial_check_only_triggers_stop_loss(monkeypatch):
    calls = {"tp": 0, "close": 0}
    pos = Position(
        id=9,
        customer_id=1,
        symbol="BTC/USDT",
        side="long",
        qty=1,
        entry_price=100,
        sl=90,
        tp_levels=[{"level": 1, "price": 110, "status": "pending"}],
        status="open",
    )

    async def fake_tp(*args, **kwargs):
        calls["tp"] += 1

    async def fake_close(*args, **kwargs):
        calls["close"] += 1
        return {"ok": True}

    monkeypatch.setattr(position_manager.order_manager, "close_at_tp_level", fake_tp)
    monkeypatch.setattr(position_manager.order_manager, "close_position", fake_close)
    await position_manager._check_one_position_with_price(FakeDb(), pos, 120, full_check=False)

    assert calls["tp"] == 0
    assert calls["close"] == 0



def test_native_stop_loss_params_okx():
    params = exchange_adapter.build_native_stop_loss_params("okx", "long", 95.0)
    assert params["reduceOnly"] is True
    assert params["posSide"] == "long"
    assert params["slTriggerPx"] == "95.0"


def test_native_stop_loss_params_binance():
    params = exchange_adapter.build_native_stop_loss_params("binance", "short", 105.0)
    assert params["reduceOnly"] is True
    assert params["stopPrice"] == 105.0
