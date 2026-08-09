"""Mock 交易所适配器 — 完全本地模拟,不需要真实 API Key。

价格通过 Binance 公开接口获取(无需 auth),带 10s 缓存。
用于本地开发测试:exchange="mock"

用法:
  在 API 管理页添加一条 exchange=mock 的记录,API Key / Secret 随意填写,
  测试连接会直接返回成功,余额固定为 100,000 USDT 模拟资金。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from loguru import logger

MOCK_BALANCE = 100_000.0

_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_CACHE_TTL = 10.0

_SYMBOL_MAP: dict[str, str] = {
    "BTC/USDT:USDT": "BTCUSDT",
    "ETH/USDT:USDT": "ETHUSDT",
    "SOL/USDT:USDT": "SOLUSDT",
    "XRP/USDT:USDT": "XRPUSDT",
    "DOGE/USDT:USDT": "DOGEUSDT",
    "BNB/USDT:USDT": "BNBUSDT",
    "ADA/USDT:USDT": "ADAUSDT",
    "AVAX/USDT:USDT": "AVAXUSDT",
    "DOT/USDT:USDT": "DOTUSDT",
    "LINK/USDT:USDT": "LINKUSDT",
    "LTC/USDT:USDT": "LTCUSDT",
    "MATIC/USDT:USDT": "MATICUSDT",
    "ARB/USDT:USDT": "ARBUSDT",
    "OP/USDT:USDT": "OPUSDT",
    "APT/USDT:USDT": "APTUSDT",
    "SUI/USDT:USDT": "SUIUSDT",
    "NEAR/USDT:USDT": "NEARUSDT",
    "ATOM/USDT:USDT": "ATOMUSDT",
    "UNI/USDT:USDT": "UNIUSDT",
    "AAVE/USDT:USDT": "AAVEUSDT",
    "ETC/USDT:USDT": "ETCUSDT",
    "TON/USDT:USDT": "TONUSDT",
    "TRX/USDT:USDT": "TRXUSDT",
    "PEPE/USDT:USDT": "PEPEUSDT",
    "WIF/USDT:USDT": "WIFUSDT",
}

_REVERSE_SYMBOL_MAP: dict[str, str] = {v: k for k, v in _SYMBOL_MAP.items()}


def _get_cached_price(symbol: str) -> float | None:
    entry = _PRICE_CACHE.get(symbol)
    if entry is None:
        return None
    price, ts = entry
    if time.time() - ts > _PRICE_CACHE_TTL:
        _PRICE_CACHE.pop(symbol, None)
        return None
    return price


def _set_cached_price(symbol: str, price: float) -> None:
    _PRICE_CACHE[symbol] = (price, time.time())


async def _fetch_binance_price(symbol: str) -> float | None:
    binance_symbol = _SYMBOL_MAP.get(symbol, symbol)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={binance_symbol}")
            data = r.json()
            if "price" in data:
                return float(data["price"])
    except Exception as e:
        logger.warning(f"Mock 获取 {symbol} 价格失败: {e}")
    return None


async def get_price(symbol: str) -> float:
    cached = _get_cached_price(symbol)
    if cached is not None:
        return cached
    price = await _fetch_binance_price(symbol)
    if price and price > 0:
        _set_cached_price(symbol, price)
        return price
    logger.warning(f"Mock {symbol} 价格获取失败,使用默认价 1.0")
    return 1.0


class MockExchange:
    """模拟 ccxt 异步交易所对象 — 所有订单即时成交,余额固定。"""

    def __init__(self):
        self._orders: dict[str, dict] = {}
        self._balance = MOCK_BALANCE
        self._leverages: dict[str, int] = {}

    async def load_markets(self):
        pass

    async def set_leverage(self, leverage: int, symbol: str):
        self._leverages[symbol] = leverage
        logger.info(f"[MOCK] set_leverage {leverage}x {symbol}")

    async def create_order(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price=None,
        params=None,
    ):
        fill_price = price or (await get_price(symbol)) or 1.0
        order_id = f"mock-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        order = {
            "id": order_id,
            "symbol": symbol,
            "type": type_,
            "side": side,
            "amount": amount,
            "price": fill_price,
            "average": fill_price,
            "filled": amount,
            "remaining": 0.0,
            "status": "closed",
            "timestamp": int(time.time() * 1000),
            "info": {},
        }
        self._orders[order_id] = order
        logger.info(f"[MOCK] create_order {side} {amount} {symbol} @ {fill_price} → {order_id}")
        return order

    async def cancel_order(self, order_id: str, symbol: str | None = None):
        if order_id in self._orders:
            self._orders[order_id]["status"] = "canceled"
        logger.info(f"[MOCK] cancel_order {order_id}")
        return {"id": order_id, "status": "canceled"}

    async def fetch_order(self, order_id: str, symbol: str | None = None):
        if order_id in self._orders:
            return self._orders[order_id]
        return {"id": order_id, "status": "closed", "filled": 0.0, "remaining": 0.0, "price": 0.0, "average": 0.0}

    async def fetch_balance(self):
        return {
            "USDT": {"total": self._balance, "free": self._balance, "used": 0.0},
            "total": {"USDT": self._balance},
            "free": {"USDT": self._balance},
            "used": {"USDT": 0.0},
        }

    def market(self, symbol: str) -> dict:
        return {"contractSize": 1, "precision": {"amount": 1, "price": 2}, "symbol": symbol}

    async def fetch_ticker(self, symbol: str) -> dict:
        price = await get_price(symbol)
        return {"last": price, "bid": price, "ask": price, "symbol": symbol}

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.2f}"

    async def fetch_positions(self) -> list:
        return []

    async def fetch_open_orders(self) -> list:
        return []

    async def close(self):
        pass

    @property
    def id(self) -> str:
        return "mock"


def create_exchange(api_key: str = "", api_secret: str = "", passphrase: str = "", is_testnet: bool = True) -> MockExchange:
    return MockExchange()


def normalize_symbol(symbol: str) -> str:
    s = symbol.upper()
    if ":USDT" in s:
        return s
    if "/" in s:
        return s
    base = s.replace("USDT", "") if s.endswith("USDT") else s
    return f"{base}/USDT:USDT"


def open_params(direction: str) -> dict:
    return {}


def close_params(direction: str) -> dict:
    return {"reduceOnly": True}


async def get_balance(api_key: str = "", api_secret: str = "", passphrase: str = "", is_testnet: bool = True) -> float:
    return MOCK_BALANCE
