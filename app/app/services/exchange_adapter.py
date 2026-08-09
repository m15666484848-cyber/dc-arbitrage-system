"""交易所适配器:基于 ccxt 统一封装 OKX / Binance / Bybit(含测试网)。

- 加密 API Key 从数据库加载
- 下单(market/limit)、撤单、平仓、查询持仓/余额/行情
- 自动设置杠杆与保证金模式
- 内置重试机制(指数退避)
- 价格行情带 Redis 缓存(TTL 5s) + 进程内限流器
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.models.config import ExchangeAccount


MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 10.0
PRICE_CACHE_TTL = 5
PRICE_CACHE_MAXSIZE = 200

try:
    from cachetools import TTLCache
    _price_cache: TTLCache = TTLCache(maxsize=PRICE_CACHE_MAXSIZE, ttl=PRICE_CACHE_TTL)
except ImportError:
    # Fallback: 使用普通 dict + 手动过期(如果 cachetools 未安装)
    _price_cache: dict[str, tuple[float, float]] = {}
    logger.debug("cachetools 未安装,使用手动缓存管理")


_rate_limiters: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "last_call": 0.0,
    "min_interval": 0.08,
})

_EXCHANGE_SYMBOLS: dict[str, set[str]] = defaultdict(set)


async def _retry_with_backoff(func, *args, retries=MAX_RETRIES, **kwargs):
    last_error = None
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection) as e:
            last_error = e
            if attempt < retries - 1:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                logger.warning(
                    f"交易所 API 调用失败(尝试 {attempt + 1}/{retries}): {e}, "
                    f"{delay:.1f} 秒后重试..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"交易所 API 调用失败(已重试 {retries} 次): {e}")
                raise
        except ccxt.InsufficientFunds as e:
            logger.error(f"余额不足: {e}")
            raise ValueError(f"交易所余额不足，请充值后重试") from e
        except ccxt.InvalidOrder as e:
            logger.error(f"订单参数错误: {e}")
            raise ValueError(f"订单参数无效: {e}") from e
    raise last_error


def _create_exchange(exchange: str, api_key: str, api_secret: str, passphrase: str, testnet: bool):
    ex_cls = {
        "okx": ccxt.okx,
        "binance": ccxt.binance,
        "bybit": ccxt.bybit,
    }.get(exchange)
    if not ex_cls:
        raise ValueError(f"不支持的交易所: {exchange}")
    kwargs: dict[str, Any] = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
    if exchange == "okx" and passphrase:
        kwargs["password"] = passphrase
    ex = ex_cls(kwargs)
    if testnet:
        ex.set_sandbox_mode(True)
    return ex


async def load_exchange(db: AsyncSession, customer_id: int, exchange: str, testnet: bool | None = None):
    stmt = select(ExchangeAccount).where(
        ExchangeAccount.customer_id == customer_id,
        ExchangeAccount.exchange == exchange,
        ExchangeAccount.is_active.is_(True),
    )
    if testnet is not None:
        stmt = stmt.where(ExchangeAccount.testnet.is_(testnet))
    result = await db.execute(stmt)
    acc = result.scalars().first()
    if not acc:
        raise ValueError(f"未配置 {exchange} 交易所账号")
    api_key = decrypt_secret(acc.api_key_enc)
    api_secret = decrypt_secret(acc.api_secret_enc)
    passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
    ex = _create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet)
    try:
        await ex.load_markets()
    except Exception as e:
        await ex.close()
        raise ValueError(f"加载 {exchange} 市场数据失败: {e}") from e
    return ex, acc


def _get_cached_price(cache_key: str) -> float | None:
    # TTLCache 自动处理过期,无需手动检查
    if hasattr(_price_cache, 'ttl'):
        # cachetools TTLCache
        entry = _price_cache.get(cache_key)
        if entry is None:
            return None
        # TTLCache 存储纯值,兼容旧格式 (price, ts)
        if isinstance(entry, tuple):
            return entry[0]
        return entry
    else:
        # 手动缓存 fallback
        entry = _price_cache.get(cache_key)
        if entry is None:
            return None
        price, ts = entry
        if time.time() - ts > PRICE_CACHE_TTL:
            _price_cache.pop(cache_key, None)
            return None
        return price


def _set_cached_price(cache_key: str, price: float) -> None:
    # TTLCache 自动处理过期和容量,无需手动清理
    if hasattr(_price_cache, 'ttl'):
        _price_cache[cache_key] = price
    else:
        _price_cache[cache_key] = (price, time.time())
        if len(_price_cache) > PRICE_CACHE_MAXSIZE:
            now = time.time()
            stale = [k for k, (_, ts) in _price_cache.items() if now - ts > PRICE_CACHE_TTL]
            for k in stale:
                _price_cache.pop(k, None)


async def _rate_limit_wait(exchange: str) -> None:
    limiter = _rate_limiters[exchange]
    now = time.monotonic()
    elapsed = now - limiter["last_call"]
    if elapsed < limiter["min_interval"]:
        await asyncio.sleep(limiter["min_interval"] - elapsed)
    limiter["last_call"] = time.monotonic()


async def fetch_ticker(exchange: str, symbol: str) -> float | None:
    """公开行情(无需 API Key),带进程内缓存(TTL 5s)和限流器。"""
    cache_key = f"{exchange}:{symbol}"
    cached = _get_cached_price(cache_key)
    if cached is not None:
        return cached

    ex_cls = {"okx": ccxt.okx, "binance": ccxt.binance, "bybit": ccxt.bybit}.get(exchange)
    if not ex_cls:
        return None
    await _rate_limit_wait(exchange)
    ex = ex_cls({"enableRateLimit": True})
    try:
        ticker = await ex.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price and price > 0:
            _set_cached_price(cache_key, float(price))
        return float(price) if price else None
    except Exception as e:
        logger.warning(f"获取 {exchange} {symbol} 行情失败: {e}")
        return None
    finally:
        await ex.close()


async def fetch_tickers_batch(exchange: str, symbols: list[str]) -> dict[str, float]:
    """批量获取多个品种的行情,带缓存和限流器。"""
    result: dict[str, float] = {}
    uncached: list[str] = []

    for sym in symbols:
        cache_key = f"{exchange}:{sym}"
        cached = _get_cached_price(cache_key)
        if cached is not None:
            result[sym] = cached
        else:
            uncached.append(sym)

    if not uncached:
        return result

    ex_cls = {"okx": ccxt.okx, "binance": ccxt.binance, "bybit": ccxt.bybit}.get(exchange)
    if not ex_cls:
        return result
    await _rate_limit_wait(exchange)
    ex = ex_cls({"enableRateLimit": True})
    try:
        try:
            tickers = await ex.fetch_tickers(uncached)
            for sym in uncached:
                ticker = tickers.get(sym)
                if ticker:
                    price = ticker.get("last") or ticker.get("close")
                    if price and price > 0:
                        p = float(price)
                        result[sym] = p
                        _set_cached_price(f"{exchange}:{sym}", p)
        except Exception:
            for sym in uncached:
                try:
                    await _rate_limit_wait(exchange)
                    ticker = await ex.fetch_ticker(sym)
                    price = ticker.get("last") or ticker.get("close")
                    if price and price > 0:
                        p = float(price)
                        result[sym] = p
                        _set_cached_price(f"{exchange}:{sym}", p)
                except Exception as e:
                    logger.warning(f"获取 {exchange} {sym} 行情失败: {e}")
    except Exception as e:
        logger.warning(f"批量获取 {exchange} 行情失败: {e}")
    finally:
        await ex.close()

    return result


async def fetch_market_price(exchange: str, symbol: str) -> float | None:
    """获取当前市价(带缓存)。"""
    return await fetch_ticker(exchange, symbol)


async def fetch_market_prices_batch(exchange: str, symbols: list[str]) -> dict[str, float]:
    """批量获取市价(带缓存)。"""
    return await fetch_tickers_batch(exchange, symbols)


async def validate_symbol(exchange: str, symbol: str) -> bool:
    """校验品种在交易所是否存在。"""
    ex_cls = {"okx": ccxt.okx, "binance": ccxt.binance, "bybit": ccxt.bybit}.get(exchange)
    if not ex_cls:
        return False
    ex = ex_cls({"enableRateLimit": True})
    try:
        await ex.load_markets()
        return symbol in ex.symbols
    except Exception:
        return False
    finally:
        await ex.close()


async def set_leverage(ex, symbol: str, leverage: int) -> None:
    try:
        if hasattr(ex, "set_leverage"):
            await ex.set_leverage(leverage, symbol)
    except Exception as e:
        logger.warning(f"设置杠杆失败 {symbol} {leverage}x: {e}")


async def place_order(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    amount: float,
    price: float | None = None,
    leverage: int = 1,
    reduce_only: bool = False,
    retries: int = MAX_RETRIES,
) -> dict:
    await set_leverage(ex, symbol, leverage)

    if price and order_type == "limit" and hasattr(ex, "price_to_precision"):
        try:
            price = float(ex.price_to_precision(symbol, price))
        except Exception as e:
            logger.warning(f"价格精度调整失败: {e}")

    if hasattr(ex, "amount_to_precision"):
        try:
            amount = float(ex.amount_to_precision(symbol, amount))
        except Exception as e:
            logger.warning(f"数量精度调整失败: {e}")

    params: dict[str, Any] = {}
    if reduce_only and ex.id not in ("okx",):
        params["reduceOnly"] = True

    async def _create_order():
        return await ex.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            price=price if order_type == "limit" else None,
            params=params,
        )

    order = await _retry_with_backoff(_create_order, retries=retries)

    status = order.get("status")
    filled = float(order.get("filled", 0) or 0)
    if status == "closed" or (status == "open" and filled > 0):
        logger.info(
            f"订单创建成功: {symbol} {side} {order_type} "
            f"数量:{amount} 成交:{filled} 状态:{status}"
        )
    else:
        logger.warning(f"订单状态异常: {order}")

    return order


async def cancel_order(ex, order_id: str, symbol: str) -> dict:
    return await ex.cancel_order(order_id, symbol)


async def fetch_positions(ex) -> list[dict]:
    try:
        positions = await ex.fetch_positions()
        return [p for p in positions if float(p.get("contracts", 0) or 0) > 0]
    except Exception as e:
        logger.warning(f"获取持仓失败: {e}")
        return []


async def fetch_balance(ex) -> dict:
    balance = await ex.fetch_balance()
    info = balance.get("info", {})
    for key in ("totalEq", "totalWalletBalance", "totalMarginBalance"):
        if key in info and info[key]:
            try:
                return {"equity": float(info[key]), "balance": float(info.get("totalWalletBalance", info[key]))}
            except (ValueError, TypeError):
                continue
    usdt = balance.get("USDT", {})
    total = usdt.get("total", 0) or 0
    return {"equity": total, "balance": total}


async def close_position_market(ex, symbol: str, side: str, amount: float) -> dict:
    close_side = "sell" if side == "long" else "buy"
    return await place_order(
        ex, symbol, close_side, "market", amount, leverage=1, reduce_only=True
    )


async def close_exchange(ex) -> None:
    try:
        await ex.close()
    except Exception:
        pass