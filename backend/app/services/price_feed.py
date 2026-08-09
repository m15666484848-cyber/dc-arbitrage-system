"""行情源封装。

price_feed_mode=polling 时使用 REST 批量行情。
price_feed_mode=websocket 时优先尝试公共 WebSocket ticker,失败或超时自动回退 REST。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import websockets
from loguru import logger

from app.core.config import settings
from app.services import exchange_adapter

WS_TIMEOUT_SECONDS = 2.5
WS_CACHE_TTL_SECONDS = 3.0
_ws_price_cache: dict[tuple[str, str], tuple[float, float]] = {}
_ws_fallback_logged: set[str] = set()


def _get_cached(exchange: str, symbol: str) -> float | None:
    item = _ws_price_cache.get((exchange, symbol))
    if not item:
        return None
    price, ts = item
    if time.monotonic() - ts > WS_CACHE_TTL_SECONDS:
        _ws_price_cache.pop((exchange, symbol), None)
        return None
    return price


def _set_cached(exchange: str, symbol: str, price: float) -> None:
    if price > 0:
        _ws_price_cache[(exchange, symbol)] = (price, time.monotonic())


def _okx_inst_id(symbol: str) -> str:
    base = symbol.replace("/", "-").replace(":USDT", "")
    if not base.endswith("-SWAP"):
        base = f"{base}-SWAP"
    return base


async def _fetch_okx_ws(symbols: list[str]) -> dict[str, float]:
    inst_to_symbol = {_okx_inst_id(sym): sym for sym in symbols}
    args = [{"channel": "tickers", "instId": inst} for inst in inst_to_symbol]
    async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public", ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        deadline = time.monotonic() + WS_TIMEOUT_SECONDS
        out: dict[str, float] = {}
        while time.monotonic() < deadline and len(out) < len(inst_to_symbol):
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            data = json.loads(msg)
            for item in data.get("data", []) or []:
                inst = item.get("instId")
                sym = inst_to_symbol.get(inst)
                if not sym:
                    continue
                price = float(item.get("last") or 0)
                if price > 0:
                    out[sym] = price
        return out


async def _fetch_binance_ws(symbols: list[str]) -> dict[str, float]:
    streams = []
    stream_to_symbol = {}
    for sym in symbols:
        stream = sym.replace("/USDT", "USDT").replace(":USDT", "").lower() + "@ticker"
        streams.append(stream)
        stream_to_symbol[stream] = sym
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)
    async with websockets.connect(url, ping_interval=None) as ws:
        deadline = time.monotonic() + WS_TIMEOUT_SECONDS
        out: dict[str, float] = {}
        while time.monotonic() < deadline and len(out) < len(symbols):
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            data = json.loads(msg)
            stream = data.get("stream")
            sym = stream_to_symbol.get(stream)
            if not sym:
                continue
            price = float((data.get("data") or {}).get("c") or 0)
            if price > 0:
                out[sym] = price
        return out


async def _fetch_bybit_ws(symbols: list[str]) -> dict[str, float]:
    args = [f"tickers.{sym.replace('/USDT', 'USDT').replace(':USDT', '')}" for sym in symbols]
    topic_to_symbol = dict(zip(args, symbols))
    async with websockets.connect("wss://stream.bybit.com/v5/public/linear", ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": args}))
        deadline = time.monotonic() + WS_TIMEOUT_SECONDS
        out: dict[str, float] = {}
        while time.monotonic() < deadline and len(out) < len(symbols):
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            data = json.loads(msg)
            sym = topic_to_symbol.get(data.get("topic"))
            if not sym:
                continue
            price = float((data.get("data") or {}).get("lastPrice") or 0)
            if price > 0:
                out[sym] = price
        return out


async def _fetch_ws(exchange: str, symbols: list[str]) -> dict[str, float]:
    ex = exchange.lower()
    if ex == "okx":
        return await _fetch_okx_ws(symbols)
    if ex == "binance":
        return await _fetch_binance_ws(symbols)
    if ex == "bybit":
        return await _fetch_bybit_ws(symbols)
    raise ValueError(f"暂不支持 WebSocket 行情源: {exchange}")


async def fetch_market_prices(exchange: str, symbols: list[str]) -> dict[str, float]:
    clean_symbols = list(dict.fromkeys(symbols))
    if settings.price_feed_mode != "websocket":
        return await exchange_adapter.fetch_market_prices_batch(exchange, clean_symbols)

    out: dict[str, float] = {}
    missing: list[str] = []
    for sym in clean_symbols:
        cached = _get_cached(exchange, sym)
        if cached and cached > 0:
            out[sym] = cached
        else:
            missing.append(sym)
    if missing:
        try:
            ws_prices = await _fetch_ws(exchange, missing)
            for sym, price in ws_prices.items():
                if price > 0:
                    out[sym] = price
                    _set_cached(exchange, sym, price)
        except Exception as e:
            if exchange not in _ws_fallback_logged:
                logger.warning(f"WebSocket 行情失败,回退 REST: exchange={exchange} err={e}")
                _ws_fallback_logged.add(exchange)
    still_missing = [sym for sym in clean_symbols if sym not in out]
    if still_missing:
        rest_prices = await exchange_adapter.fetch_market_prices_batch(exchange, still_missing)
        out.update({sym: price for sym, price in rest_prices.items() if price and price > 0})
    return out
