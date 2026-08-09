"""交易所适配器:基于 ccxt 统一封装 OKX / Binance / Bybit(含测试网)。

- 加密 API Key 从数据库加载
- 下单(market/limit)、撤单、平仓、查询持仓/余额/行情
- 自动设置杠杆与保证金模式
- 内置重试机制(指数退避)
- 价格行情带 Redis 缓存(TTL 5s) + 进程内限流器
- OKX 原生 REST 余额查询(比 ccxt 更快更轻量)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

import ccxt.async_support as ccxt
import httpx
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
    _price_cache: dict[str, tuple[float, float]] = {}
    logger.debug("cachetools 未安装,使用手动缓存管理")


_rate_limiters: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "last_call": 0.0,
    "min_interval": 0.08,
})
# 限流锁:防止并发请求竞态导致间隔检查失效(多个协程同时读到旧 last_call)
_rate_locks: dict[str, "asyncio.Lock"] = defaultdict(asyncio.Lock)

# 公开行情查询实例缓存(无 API Key,复用 HTTP 连接池)
_public_exchanges: dict[str, Any] = {}

# 私有交易实例短缓存:减少频繁平仓/撤单时重复创建 ccxt 实例和 load_markets。
# key=exchange_account_id,value=(exchange_instance, expires_at)
PRIVATE_EXCHANGE_CACHE_TTL = 60.0
_private_exchange_cache: dict[int, tuple[Any, float]] = {}
_private_exchange_locks: dict[int, "asyncio.Lock"] = defaultdict(asyncio.Lock)

_EXCHANGE_SYMBOLS: dict[str, set[str]] = defaultdict(set)


def _normalize_symbol(exchange: str, symbol: str) -> str:
    """将系统内部 symbol 格式转换为交易所所需的格式。

    系统内部统一使用 "BTC/USDT" 格式(SPOT 风格),但:
    - OKX SWAP(永续合约)需要 "BTC/USDT:USDT" 格式
    - OKX SPOT 使用 "BTC/USDT" 格式
    - Binance/Bybit SWAP 使用 "BTC/USDT" 或 "BTC/USDT:USDT"

    对于 OKX,如果 symbol 是 "BTC/USDT"(无 ":USDT" 后缀),自动转为 "BTC/USDT:USDT"。
    因为跟单系统统一使用合约(SWAP)账户,需要 posSide 等合约参数。

    如果 symbol 已经是 "BTC/USDT:USDT" 格式,则保持不变。
    """
    if not symbol:
        return symbol
    # 已经是合约格式(包含 ":")直接返回
    if ":" in symbol:
        return symbol
    # 合约账户统一使用 "BTC/USDT:USDT" 格式。
    # OKX / Binance U本位 / Bybit USDT 永续在 ccxt 中都可用该统一符号定位 swap market。
    if exchange.lower() in ("okx", "binance", "bybit") and "/USDT" in symbol:
        return f"{symbol}:USDT"
    return symbol


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
    if exchange == "okx":
        kwargs["options"] = {"defaultType": "swap"}
    elif exchange == "binance":
        # DCQuant 只做 USDT 永续合约。币安不指定 defaultType 时 ccxt 默认读现货,
        # 会导致合约账户余额显示 0 或下单/持仓接口权限不匹配。
        kwargs["options"] = {
            "defaultType": "future",
            "adjustForTimeDifference": True,
            "fetchOpenOrders": {"warnWithoutSymbol": False},
        }
    elif exchange == "bybit":
        kwargs["options"] = {"defaultType": "swap"}
    if exchange == "okx" and passphrase:
        kwargs["password"] = passphrase
    ex = ex_cls(kwargs)
    if testnet:
        ex.set_sandbox_mode(True)
    return ex


async def load_exchange(
    db: AsyncSession,
    customer_id: int,
    exchange: str | None = None,
    testnet: bool | None = None,
    exchange_account_id: int | None = None,
    use_cache: bool = True,
):
    if exchange_account_id is not None:
        stmt = select(ExchangeAccount).where(
            ExchangeAccount.id == exchange_account_id,
            ExchangeAccount.customer_id == customer_id,
            ExchangeAccount.is_active.is_(True),
        )
    else:
        if not exchange:
            raise ValueError("未指定交易所")
        stmt = select(ExchangeAccount).where(
            ExchangeAccount.customer_id == customer_id,
            ExchangeAccount.exchange == exchange,
            ExchangeAccount.is_active.is_(True),
        )
        if testnet is not None:
            stmt = stmt.where(ExchangeAccount.testnet.is_(testnet))
        # 多 API 场景:
        # 1. 优先使用客户设置的默认下单 API
        # 2. 避免优先选择最近验证失败的备用 API
        # 3. 同条件下优先使用最近验证成功的账号,最后按 id 保持稳定兜底
        stmt = stmt.order_by(
            ExchangeAccount.is_default.desc(),
            ExchangeAccount.last_error.asc(),
            ExchangeAccount.last_verified_at.desc().nullslast(),
            ExchangeAccount.id,
        )
    result = await db.execute(stmt)
    acc = result.scalars().first()
    if not acc:
        raise ValueError(f"未配置 {exchange or exchange_account_id} 交易所账号")
    if use_cache and acc.id:
        cached = _private_exchange_cache.get(acc.id)
        now = time.monotonic()
        if cached and cached[1] > now:
            ex = cached[0]
            setattr(ex, "_dcq_cached_private", True)
            return ex, acc
        async with _private_exchange_locks[acc.id]:
            cached = _private_exchange_cache.get(acc.id)
            now = time.monotonic()
            if cached and cached[1] > now:
                ex = cached[0]
                setattr(ex, "_dcq_cached_private", True)
                return ex, acc
            api_key = decrypt_secret(acc.api_key_enc)
            api_secret = decrypt_secret(acc.api_secret_enc)
            passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
            ex = _create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet)
            try:
                await ex.load_markets()
            except Exception as e:
                await ex.close()
                raise ValueError(f"加载 {exchange} 市场数据失败: {e}") from e
            setattr(ex, "_dcq_cached_private", True)
            _private_exchange_cache[acc.id] = (ex, time.monotonic() + PRIVATE_EXCHANGE_CACHE_TTL)
            return ex, acc
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
    if hasattr(_price_cache, 'ttl'):
        # TTLCache 自动过期
        entry = _price_cache.get(cache_key)
        if entry is None:
            return None
        return entry if isinstance(entry, (int, float)) else entry[0]
    else:
        # fallback: 手动过期
        entry = _price_cache.get(cache_key)
        if entry is None:
            return None
        price, ts = entry
        if time.time() - ts > PRICE_CACHE_TTL:
            _price_cache.pop(cache_key, None)
            return None
        return price


def _set_cached_price(cache_key: str, price: float) -> None:
    if hasattr(_price_cache, 'ttl'):
        # TTLCache: 直接存值,自动管理过期和容量
        _price_cache[cache_key] = price
    else:
        # fallback: 手动管理
        _price_cache[cache_key] = (price, time.time())
        if len(_price_cache) > PRICE_CACHE_MAXSIZE:
            now = time.time()
            stale = [k for k, (_, ts) in _price_cache.items() if now - ts > PRICE_CACHE_TTL]
            for k in stale:
                _price_cache.pop(k, None)


async def _rate_limit_wait(exchange: str) -> None:
    # 加锁串行化同一交易所的限流检查,避免并发协程同时通过间隔检查后突发请求
    async with _rate_locks[exchange]:
        limiter = _rate_limiters[exchange]
        now = time.monotonic()
        elapsed = now - limiter["last_call"]
        if elapsed < limiter["min_interval"]:
            await asyncio.sleep(limiter["min_interval"] - elapsed)
        limiter["last_call"] = time.monotonic()




async def _get_public_exchange(exchange: str):
    """获取或创建公开行情查询实例(无 API Key),复用 HTTP 连接池。"""
    if exchange not in _public_exchanges:
        ex_cls = {"okx": ccxt.okx, "binance": ccxt.binance, "bybit": ccxt.bybit}.get(exchange)
        if not ex_cls:
            return None
        kwargs: dict[str, Any] = {"enableRateLimit": True}
        if exchange == "okx":
            kwargs["options"] = {"defaultType": "swap"}
        elif exchange == "binance":
            kwargs["options"] = {
                "defaultType": "future",
                "adjustForTimeDifference": True,
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            }
        elif exchange == "bybit":
            kwargs["options"] = {"defaultType": "swap"}
        _public_exchanges[exchange] = ex_cls(kwargs)
    return _public_exchanges[exchange]


async def fetch_ticker(exchange: str, symbol: str) -> float | None:
    """公开行情(无需 API Key),带进程内缓存(TTL 5s)和限流器。"""
    # 标准化 symbol 格式(OKX SWAP 需要 "BTC/USDT:USDT")
    symbol = _normalize_symbol(exchange, symbol)
    cache_key = f"{exchange}:{symbol}"
    cached = _get_cached_price(cache_key)
    if cached is not None:
        return cached

    ex = await _get_public_exchange(exchange)
    if not ex:
        return None
    await _rate_limit_wait(exchange)
    try:
        ticker = await ex.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price and price > 0:
            _set_cached_price(cache_key, float(price))
        return float(price) if price else None
    except Exception as e:
        logger.warning(f"获取 {exchange} {symbol} 行情失败: {e}")
        return None


async def fetch_tickers_batch(exchange: str, symbols: list[str]) -> dict[str, float]:
    """批量获取多个品种的行情,带缓存和限流器。"""
    # 标准化 symbol 格式(OKX SWAP 需要 "BTC/USDT:USDT")
    normalized = {sym: _normalize_symbol(exchange, sym) for sym in symbols}
    result: dict[str, float] = {}
    uncached: list[str] = []

    for sym in symbols:
        norm_sym = normalized[sym]
        cache_key = f"{exchange}:{norm_sym}"
        cached = _get_cached_price(cache_key)
        if cached is not None:
            result[sym] = cached
        else:
            uncached.append(norm_sym)

    if not uncached:
        return result

    ex = await _get_public_exchange(exchange)
    if not ex:
        return result
    await _rate_limit_wait(exchange)
    # 反向映射: normalized symbol → original symbol(用于结果回填)
    norm_to_orig = {normalized[sym]: sym for sym in symbols}
    try:
        try:
            tickers = await ex.fetch_tickers(uncached)
            for sym in uncached:
                ticker = tickers.get(sym)
                if ticker:
                    price = ticker.get("last") or ticker.get("close")
                    if price and price > 0:
                        p = float(price)
                        orig = norm_to_orig.get(sym, sym)
                        result[orig] = p
                        _set_cached_price(f"{exchange}:{sym}", p)
        except Exception:
            for sym in uncached:
                try:
                    await _rate_limit_wait(exchange)
                    ticker = await ex.fetch_ticker(sym)
                    price = ticker.get("last") or ticker.get("close")
                    if price and price > 0:
                        p = float(price)
                        orig = norm_to_orig.get(sym, sym)
                        result[orig] = p
                        _set_cached_price(f"{exchange}:{sym}", p)
                except Exception as e:
                    logger.warning(f"获取 {exchange} {sym} 行情失败: {e}")
    except Exception as e:
        logger.warning(f"批量获取 {exchange} 行情失败: {e}")


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
    kwargs: dict[str, Any] = {"enableRateLimit": True}
    if exchange == "okx":
        kwargs["options"] = {"defaultType": "swap"}
    elif exchange == "binance":
        kwargs["options"] = {"defaultType": "future", "adjustForTimeDifference": True}
    elif exchange == "bybit":
        kwargs["options"] = {"defaultType": "swap"}
    ex = ex_cls(kwargs)
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
    position_side: str | None = None,
) -> dict:
    """下单。

    Args:
        position_side: 持仓方向('long'/'short'),OKX 双向持仓模式(long_short_mode)
                       必须指定 posSide,否则会报 sCode=51000 "Parameter posSide error"。
                       - 开多: side=buy,  position_side=long
                       - 开空: side=sell, position_side=short
                       - 平多: side=sell, position_side=long,  reduce_only=True
                       - 平空: side=buy,  position_side=short, reduce_only=True
                       - 净持仓模式: position_side=net
                       传入后,会自动在 params 中设置 posSide。
    """
    # 推断交易所名称(用于 symbol 格式标准化)
    ex_name = getattr(ex, "id", "") or ""
    symbol = _normalize_symbol(ex_name, symbol)

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
    if reduce_only:
        # OKX 同样需要 reduceOnly(ccxt 会映射为 posSide/posSideMode 兼容参数),
        # 之前排除 OKX 会导致平仓单被当作开仓单,产生反向持仓
        params["reduceOnly"] = True
    # OKX 双向持仓模式(long_short_mode)必须指定 posSide；
    # 但净持仓模式(net_mode)传 posSide 会报 51000 Parameter posSide error。
    # 因此先按双向模式传，若交易所明确拒绝，再去掉 posSide 重试一次。
    if position_side and position_side in ("long", "short", "net"):
        params["posSide"] = position_side

    async def _create_order(order_params: dict[str, Any] | None = None):
        return await ex.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            price=price if order_type == "limit" else None,
            params=order_params if order_params is not None else params,
        )

    try:
        order = await _retry_with_backoff(lambda: _create_order(params), retries=retries)
    except Exception as e:
        msg = str(e)
        if (
            ex_name.lower() == "okx"
            and "posSide" in msg
            and "51000" in msg
            and "posSide" in params
            and reduce_only
            and params.get("posSide") in ("long", "short")
        ):
            fallback_params = dict(params)
            fallback_params["posSide"] = "net"
            logger.warning(
                f"OKX reduce-only 平仓 posSide={params.get('posSide')} 被拒,"
                f"改用净持仓 posSide=net 重试: {symbol} {side} {order_type}"
            )
            try:
                order = await _retry_with_backoff(lambda: _create_order(fallback_params), retries=1)
            except Exception as net_e:
                net_msg = str(net_e)
                if "posSide" in net_msg and "51000" in net_msg:
                    fallback_params.pop("posSide", None)
                    logger.warning(
                        f"OKX posSide=net 仍被拒,去掉 posSide 最后重试: "
                        f"{symbol} {side} {order_type}"
                    )
                    order = await _retry_with_backoff(lambda: _create_order(fallback_params), retries=1)
                else:
                    raise
        elif ex_name.lower() == "okx" and "posSide" in msg and "51000" in msg and "posSide" in params:
            fallback_params = dict(params)
            fallback_params.pop("posSide", None)
            logger.warning(
                f"OKX 当前账户可能为净持仓模式,posSide 被拒绝,去掉 posSide 重试: "
                f"{symbol} {side} {order_type}"
            )
            order = await _retry_with_backoff(lambda: _create_order(fallback_params), retries=1)
        elif (
            ex_name.lower() == "okx"
            and reduce_only
            and "51170" in msg
            and params.get("posSide") in ("long", "short")
        ):
            fallback_params = dict(params)
            fallback_params["posSide"] = "net"
            logger.warning(
                f"OKX reduce-only 方向校验失败,当前账户可能为净持仓模式,"
                f"改用 posSide=net 重试: {symbol} {side} {order_type}"
            )
            order = await _retry_with_backoff(lambda: _create_order(fallback_params), retries=1)
        else:
            raise

    status = order.get("status")
    filled = float(order.get("filled", 0) or 0)
    order_id = order.get("id")
    # OKX 市价单返回 status=None(异步成交),只要拿到 order_id 就算成功
    if status == "closed" or (status == "open" and filled > 0):
        logger.info(
            f"订单创建成功: {symbol} {side} {order_type} "
            f"数量:{amount} 成交:{filled} 状态:{status}"
        )
    elif status is None and order_id:
        logger.info(
            f"订单已提交(异步): {symbol} {side} {order_type} "
            f"数量:{amount} orderId:{order_id} 状态:pending(OKX异步成交)"
        )
    else:
        logger.warning(f"订单状态异常: {order}")

    return order


async def cancel_order(ex, order_id: str, symbol: str) -> dict:
    return await ex.cancel_order(order_id, symbol)


async def fetch_positions(ex) -> list[dict]:
    positions = await ex.fetch_positions()
    return [p for p in positions if float(p.get("contracts", 0) or 0) > 0]


async def fetch_balance(ex) -> dict:
    balance = await ex.fetch_balance()
    info = balance.get("info", {}) or {}
    available = 0.0
    for key in ("availEq", "availableEq", "availableMargin", "free", "availBal"):
        if info.get(key):
            try:
                available = float(info[key])
                break
            except (ValueError, TypeError):
                pass
    usdt = balance.get("USDT", {}) or {}
    if not available:
        available = float(usdt.get("free", 0) or balance.get("free", {}).get("USDT", 0) or 0)
    for key in ("totalEq", "totalWalletBalance", "totalMarginBalance"):
        if key in info and info[key]:
            try:
                equity = float(info[key])
                wallet = float(info.get("totalWalletBalance", info[key]) or 0)
                return {"equity": equity, "balance": wallet, "available_margin": available or wallet}
            except (ValueError, TypeError):
                continue
    total = float(usdt.get("total", 0) or 0)
    return {"equity": total, "balance": total, "available_margin": available or total}


def build_native_stop_loss_params(exchange: str, side: str, stop_price: float) -> dict:
    """构建交易所原生止损参数。默认不自动启用,供灰度开关调用。"""
    if stop_price <= 0:
        raise ValueError("止损价格必须大于 0")
    ex_name = (exchange or "").lower()
    if ex_name == "okx":
        return {
            "tdMode": "cross",
            "posSide": side,
            "reduceOnly": True,
            "slTriggerPx": str(stop_price),
            "slOrdPx": "-1",
        }
    if ex_name == "binance":
        return {
            "reduceOnly": True,
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
        }
    if ex_name == "bybit":
        return {
            "reduceOnly": True,
            "triggerPrice": stop_price,
            "triggerDirection": 2 if side == "long" else 1,
        }
    raise ValueError(f"不支持原生止损单的交易所: {exchange}")


async def place_native_stop_loss_order(
    ex,
    exchange: str,
    symbol: str,
    side: str,
    amount: float,
    stop_price: float,
) -> dict:
    """提交交易所原生止损单。

    当前实际启用 OKX 原生 algo conditional 止损单。其他交易所先显式拒绝,
    避免条件单参数差异导致小仓验证时误触发。
    """
    ex_name = (exchange or getattr(ex, "id", "") or "").lower()
    if ex_name != "okx":
        raise ValueError(f"原生止损小仓验证当前仅启用 OKX,当前交易所={exchange}")
    if stop_price <= 0:
        raise ValueError("止损价格必须大于 0")

    norm_symbol = _normalize_symbol("okx", symbol)
    market = ex.market(norm_symbol)
    inst_id = market.get("id") or norm_symbol.replace("/", "-").replace(":USDT", "-SWAP")
    contract_size = float(market.get("contractSize") or 1.0)
    sz = amount / contract_size if contract_size > 0 else amount
    if hasattr(ex, "amount_to_precision"):
        sz = ex.amount_to_precision(norm_symbol, sz)
    else:
        sz = str(sz)
    trigger_px = ex.price_to_precision(norm_symbol, stop_price) if hasattr(ex, "price_to_precision") else str(stop_price)
    close_side = "sell" if side == "long" else "buy"

    payload = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": close_side,
        "posSide": side,
        "ordType": "conditional",
        "sz": str(sz),
        "slTriggerPx": str(trigger_px),
        "slOrdPx": "-1",
        "reduceOnly": "true",
    }
    try:
        return await ex.privatePostTradeOrderAlgo(payload)
    except Exception as e:
        msg = str(e)
        if "posSide" in msg or "51000" in msg:
            fallback = dict(payload)
            fallback["posSide"] = "net"
            logger.warning(f"OKX 原生止损 posSide={side} 被拒,改用 net 重试: {inst_id}")
            return await ex.privatePostTradeOrderAlgo(fallback)
        raise


async def close_position_market(ex, symbol: str, side: str, amount: float) -> dict:
    """市价平仓。

    Args:
        side: 持仓方向('long'/'short'),用于确定平仓订单方向和 posSide。
        amount: 持仓币数(ETH/BTC 等),会自动转为合约数。
    """
    close_side = "sell" if side == "long" else "buy"
    # 将币数转为合约数(OKX contractSize 转换)
    position_side = side
    try:
        ex_name = getattr(ex, "id", "") or ""
        norm_symbol = _normalize_symbol(ex_name, symbol)
        market = ex.market(norm_symbol)
        cs = float(market.get("contractSize") or 1.0)
        if cs != 1.0 and cs > 0:
            amount = amount / cs
        if ex_name.lower() == "okx":
            try:
                positions = await ex.fetch_positions([norm_symbol])
                for pos in positions:
                    pos_symbol = pos.get("symbol") or pos.get("info", {}).get("instId")
                    pos_side = (pos.get("info", {}) or {}).get("posSide")
                    contracts = abs(float(pos.get("contracts") or pos.get("info", {}).get("pos") or 0))
                    if (pos_symbol == norm_symbol or pos_symbol == symbol) and contracts > 0 and pos_side == "net":
                        position_side = "net"
                        break
            except Exception as e:
                logger.debug(f"探测 OKX 持仓模式失败,按方向 posSide 平仓: {e}")
    except Exception:
        pass
    # OKX 双向持仓模式平仓必须传 posSide=持仓方向；净持仓模式使用 posSide=net。
    return await place_order(
        ex, symbol, close_side, "market", amount,
        leverage=1, reduce_only=True, position_side=position_side,
    )


def extract_fee_from_order(
    ex,
    order: dict,
    symbol: str,
    filled_qty: float,
    filled_price: float,
    order_type: str = "market",
) -> float:
    """从交易所订单响应中提取手续费(USDT)。

    优先使用交易所返回的实际手续费;若交易所未返回(OKX 市价单异步成交时常见),
    则按市场默认 TAKER/MAKER 费率计算。

    Args:
        ex: ccxt 交易所实例(已加载 markets)
        order: ccxt create_order / fetch_order 返回的订单字典
        symbol: 原始 symbol(未标准化),如 "BTC/USDT"
        filled_qty: 成交数量
        filled_price: 成交均价
        order_type: "market" (TAKER) 或 "limit" (MAKER)

    Returns: 手续费(USDT)
    """
    # 1. 尝试从订单响应中提取实际手续费
    fee_info = order.get("fee") or {}
    fee_cost = float(fee_info.get("cost") or 0)
    fee_currency = (fee_info.get("currency") or "").upper()

    if fee_cost > 0:
        # 手续费以 USDT/USDC/USD 结算 → 直接使用
        if fee_currency in ("USDT", "USDC", "USD", ""):
            return fee_cost
        # 手续费以基础币种结算(如 ETH) → 转换为 USDT
        if fee_currency and filled_price > 0:
            return fee_cost * filled_price
        return fee_cost

    # 2. 交易所未返回手续费 → 按市场默认费率计算
    try:
        ex_name = getattr(ex, "id", "") or ""
        norm_symbol = _normalize_symbol(ex_name, symbol)
        market = ex.market(norm_symbol)
        if order_type == "limit":
            rate = float(market.get("maker") or 0.0005)
        else:
            rate = float(market.get("taker") or 0.001)
        notional = filled_qty * filled_price
        return notional * rate
    except Exception as e:
        logger.debug(f"按市场费率计算手续费失败 {symbol}: {e}, 使用默认 taker 0.1%")
        return filled_qty * filled_price * 0.001


async def invalidate_exchange_cache(exchange_account_id: int | None = None) -> None:
    # 剔除并关闭私有交易实例缓存。exchange_account_id=None 时清空全部。
    ids = list(_private_exchange_cache.keys()) if exchange_account_id is None else [exchange_account_id]
    for acc_id in ids:
        cached = _private_exchange_cache.pop(acc_id, None)
        if not cached:
            continue
        try:
            await cached[0].close()
        except Exception:
            pass


async def close_exchange(ex) -> None:
    try:
        if getattr(ex, "_dcq_cached_private", False):
            return
        await ex.close()
    except Exception:
        pass


# OKX 模拟交易使用相同域名 + x-simulated-trading: 1 请求头区分
# 参考: https://www.okx.com/docs-v5/en/#overview-demo-trading-services
# 两个 URL 故意相同,仅靠 header 区分,请勿"修复"为不同 URL
OKX_BASE_URL = "https://www.okx.com"
OKX_SANDBOX_URL = "https://www.okx.com"  # 故意与 BASE_URL 相同


def _okx_sign(ts: str, method: str, path: str, secret: str, body: str = "") -> str:
    message = ts + method + path + body
    mac = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


async def okx_native_balance(api_key: str, api_secret: str, passphrase: str, testnet: bool = False) -> dict:
    """OKX 原生 REST 余额查询(比 ccxt 快 ~3x,跳过市场数据加载)。

    GET /api/v5/account/balance
    返回 {"equity": float, "balance": float}
    """
    base = OKX_SANDBOX_URL if testnet else OKX_BASE_URL
    path = "/api/v5/account/balance"
    # OKX 要求 ISO8601 毫秒级时间戳;time.strftime 不支持毫秒(%03d 会被当作日),
    # 必须用 datetime 拼接
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    sign = _okx_sign(ts, "GET", path, api_secret)

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    # 测试网(模拟交易)必须带 x-simulated-trading: 1,否则会查询到真实账户
    if testnet:
        headers["x-simulated-trading"] = "1"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{base}{path}", headers=headers)
            data = r.json()
            if data.get("code") == "0":
                balances = data.get("data", [{}])
                if balances:
                    account = balances[0]
                    total_eq = float(account.get("totalEq") or 0)
                    total_wb = float(account.get("totalWalletBalance") or 0)
                    available = float(account.get("availEq") or account.get("availableEq") or 0)
                    details = account.get("details") or []
                    if not available and details:
                        for item in details:
                            ccy = (item.get("ccy") or "").upper()
                            if ccy in ("USDT", "USDC", "USD"):
                                available = float(item.get("availBal") or item.get("availEq") or 0)
                                break
                    return {"equity": total_eq or total_wb, "balance": total_wb, "available_margin": available or total_wb}
            logger.warning(f"OKX 原生余额查询失败: {data.get('msg', 'unknown')}")
            return {"equity": 0.0, "balance": 0.0, "available_margin": 0.0}
    except Exception as e:
        logger.warning(f"OKX 原生余额查询异常: {e}")
        return {"equity": 0.0, "balance": 0.0, "available_margin": 0.0}


async def fetch_balance_fast(
    exchange: str,
    db: AsyncSession,
    customer_id: int,
    testnet: bool | None = None,
    exchange_account_id: int | None = None,
) -> dict:
    """快速余额查询:OKX 用原生 REST,其他用 ccxt。"""
    if exchange == "okx":
        if exchange_account_id is not None:
            stmt = select(ExchangeAccount).where(
                ExchangeAccount.id == exchange_account_id,
                ExchangeAccount.customer_id == customer_id,
                ExchangeAccount.is_active.is_(True),
            )
        else:
            stmt = select(ExchangeAccount).where(
                ExchangeAccount.customer_id == customer_id,
                ExchangeAccount.exchange == exchange,
                ExchangeAccount.is_active.is_(True),
            )
            if testnet is not None:
                stmt = stmt.where(ExchangeAccount.testnet.is_(testnet))
            stmt = stmt.order_by(
                ExchangeAccount.is_default.desc(),
                ExchangeAccount.last_error.asc(),
                ExchangeAccount.last_verified_at.desc().nullslast(),
                ExchangeAccount.id,
            )
        acc = (await db.execute(stmt)).scalars().first()
        if not acc:
            return {"equity": 0.0, "balance": 0.0, "available_margin": 0.0}
        api_key = decrypt_secret(acc.api_key_enc)
        api_secret = decrypt_secret(acc.api_secret_enc)
        passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
        return await okx_native_balance(api_key, api_secret, passphrase, acc.testnet)
    else:
        ex, _ = await load_exchange(db, customer_id, exchange, testnet, exchange_account_id=exchange_account_id)
        try:
            return await fetch_balance(ex)
        finally:
            await close_exchange(ex)


async def fetch_okx_positions_native(api_key: str, api_secret: str, passphrase: str, testnet: bool = False) -> list:
    """OKX 原生 REST 持仓查询。"""
    base = OKX_SANDBOX_URL if testnet else OKX_BASE_URL
    path = "/api/v5/account/positions"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    sign = _okx_sign(ts, "GET", path, api_secret)

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    # 测试网(模拟交易)必须带 x-simulated-trading: 1
    if testnet:
        headers["x-simulated-trading"] = "1"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{base}{path}", headers=headers)
            data = r.json()
            if data.get("code") == "0":
                return data.get("data", [])
            return []
    except Exception as e:
        logger.warning(f"OKX 原生持仓查询异常: {e}")
        return []
