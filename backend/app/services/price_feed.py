"""行情源封装。

默认使用 REST 批量轮询。price_feed_mode=websocket 时预留 WebSocket 行情源入口,
当前未完成交易所级订阅适配前会自动回退到 REST,避免影响实盘。
"""
from __future__ import annotations

from loguru import logger

from app.core.config import settings
from app.services import exchange_adapter

_ws_fallback_logged = False


async def fetch_market_prices(exchange: str, symbols: list[str]) -> dict[str, float]:
    global _ws_fallback_logged
    if settings.price_feed_mode == "websocket" and not _ws_fallback_logged:
        logger.warning("price_feed_mode=websocket 已配置,但 WebSocket 订阅适配未启用,当前回退到 REST 批量行情")
        _ws_fallback_logged = True
    return await exchange_adapter.fetch_market_prices_batch(exchange, symbols)
