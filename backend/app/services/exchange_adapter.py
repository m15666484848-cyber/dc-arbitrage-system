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


# 交易所最小下单金额缓存(避免每次都load_markets)
_MIN_NOTIONAL_CACHE: dict[str, dict[str, float]] = {}


async def get_min_order_notional(ex, symbol: str) -> tuple[float, str]:
    """获取交易所指定交易对的最小下单金额(USDT)。

    返回 (min_notional_usdt, reason)。
    如果无法获取,返回 (0, "") 表示不做限制。

    各交易所常见限制:
    - Binance USDT-M: min notional 通常 $5 (部分币 $20)
    - Bybit: 按 min qty * price 计算
    - OKX: 按 min size * price 计算
    """
    ex_name = getattr(ex, "id", "") or ""
    norm_symbol = _normalize_symbol(ex_name, symbol)

    # 查缓存
    cache_key = f"{ex_name}:{norm_symbol}"
    if cache_key in _MIN_NOTIONAL_CACHE:
        cached = _MIN_NOTIONAL_CACHE[cache_key]
        return (cached.get("min_notional", 0), cached.get("reason", ""))

    try:
        await ex.load_markets()
        market = ex.market(norm_symbol)
    except Exception as e:
        logger.debug(f"获取market数据失败,跳过最小金额校验: {ex_name} {norm_symbol} {e}")
        return (0, "")

    min_notional = 0.0
    min_qty = 0.0
    reason = ""

    # 尝试从 limits 中获取最小下单金额
    limits = market.get("limits", {})
    cost_limits = limits.get("cost", {})
    amount_limits = limits.get("amount", {})

    # 1. 优先使用 limits.cost.min (最小名义价值)
    if cost_limits and cost_limits.get("min"):
        try:
            min_notional = float(cost_limits["min"])
        except (ValueError, TypeError):
            pass

    # 2. 如果没有 cost.min,尝试用 amount.min * 当前价格 估算
    if min_notional <= 0 and amount_limits and amount_limits.get("min"):
        try:
            min_qty = float(amount_limits["min"])
        except (ValueError, TypeError):
            pass

    # 3. 交易所特定兜底值
    if min_notional <= 0 and min_qty <= 0:
        if ex_name.lower() == "binance":
            # Binance USDT-M futures 最小名义价值通常是 5 USDT
            min_notional = 5.0
            reason = "Binance最小下单额5USDT(兜底)"
        elif ex_name.lower() == "bybit":
            # Bybit 最小下单量因币而异,用 0.001 BTC * price 估算
            min_qty = 0.001
            reason = "Bybit最小下单量0.001(兜底)"
        elif ex_name.lower() == "okx":
            # OKX 最小下单额通常是 1 USDT
            min_notional = 1.0
            reason = "OKX最小下单额1USDT(兜底)"

    # 如果有 min_qty 但没有 min_notional,用 ticker 价格估算
    if min_notional <= 0 and min_qty > 0:
        try:
            ticker = await ex.fetch_ticker(norm_symbol)
            price = ticker.get("last", 0) or ticker.get("close", 0)
            if price > 0:
                min_notional = min_qty * price
        except Exception:
            pass

    if reason == "" and min_notional > 0:
        reason = f"{ex_name.upper()}最小下单额{min_notional:.2f}USDT"

    # 写缓存
    _MIN_NOTIONAL_CACHE[cache_key] = {"min_notional": min_notional, "reason": reason}

    return (min_notional, reason)



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
                logger.error(f"交易所 API 调用失败(已重试 {retries} 次): {type(e).__name__}")
                logger.debug(f"交易所 API 详细错误: {e}")
                raise
        except ccxt.InsufficientFunds as e:
            logger.error(f"余额不足: {type(e).__name__}")
            raise ValueError(f"交易所余额不足，请充值后重试") from e
        except ccxt.InvalidOrder as e:
            logger.error(f"订单参数错误: {type(e).__name__}")
            raise ValueError(f"订单参数无效: {e}") from e
    # BUG-15 修复: retries=0 时 range(0) 不执行循环,last_error 为 None,raise None 会 TypeError
    if last_error is None:
        raise RuntimeError("No retries attempted")
    raise last_error


def _create_exchange(
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: str,
    testnet: bool,
    account_mode: str | None = None,
):
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
            "fetchMarkets": {"types": ["linear"]},
            "fetchMargins": False,
        }
    elif exchange == "bybit":
        kwargs["options"] = {
            "defaultType": "swap",
            "defaultSubType": "linear",
            "defaultSettle": "USDT",
        }
    if exchange == "okx" and passphrase:
        kwargs["password"] = passphrase
    ex = ex_cls(kwargs)
    mode = account_mode or ("testnet" if testnet else "live")
    if mode == "demo":
        if exchange in ("bybit", "binance") and hasattr(ex, "enable_demo_trading"):
            # Bybit/Binance Demo Trading 使用独立 demo 域名,不同于交易所测试网。
            ex.enable_demo_trading(True)
        else:
            raise ValueError(f"{exchange} 不支持 Demo Trading 模式")
    elif testnet or mode == "testnet":
        if exchange == "binance":
            # ccxt 4.x 已禁止 Binance futures sandbox 签名请求,但仍保留 test URL。
            # 手动切换到 U 本位合约测试网域名,避免 sign() 因 sandboxMode 抛 NotSupported。
            ex.urls["api"] = ex.urls.get("test", ex.urls["api"])
            ex.options["sandboxMode"] = False
            # Binance futures testnet 没有 sapi 钱包接口；load_markets 若拉 currencies 会失败。
            ex.has["fetchCurrencies"] = False
            async def _empty_currencies(params=None):
                return {}
            ex.fetch_currencies = _empty_currencies
        else:
            ex.set_sandbox_mode(True)
    return ex


import time as _time_module

# S14修复: 模块级缓存，避免重复 API 调用
_okx_mode_cache: dict[str, float] = {}
_OKX_MODE_CACHE_TTL = 3600  # 1小时缓存


async def _ensure_okx_long_short_mode(ex) -> None:
    """尽量确保 OKX 使用双向持仓模式。

    DCQuant 本地仓位模型按 long/short 分开记录；OKX net_mode 会把反向开仓自动冲抵，
    导致交易所净仓与本地分仓不一致。若已有持仓导致交易所拒绝切换，只记录告警；
    后续开仓时仍会通过 posSide 强约束，避免静默降级为 net_mode。

    S14修复: 先查询当前持仓模式，已是 long_short_mode 则跳过；
    切换失败后缓存1小时，避免重复 API 调用和日志告警。
    """
    if (getattr(ex, "id", "") or "").lower() != "okx":
        return

    # S14修复: 使用缓存避免重复检查（1小时TTL）
    api_key = getattr(ex, "apiKey", "") or ""
    cache_key = f"okx_mode:{api_key[:8]}" if api_key else "okx_mode:default"
    now = _time_module.time()
    cached_at = _okx_mode_cache.get(cache_key, 0)
    if now - cached_at < _OKX_MODE_CACHE_TTL:
        return  # 缓存期内跳过

    # S14修复: 先查询当前持仓模式
    try:
        if hasattr(ex, "private_get_account_config"):
            config = await ex.private_get_account_config()
            data = config.get("data", [{}]) if isinstance(config, dict) else []
            if data and isinstance(data, list):
                current_mode = data[0].get("posMode", "")
                if current_mode == "long_short_mode":
                    _okx_mode_cache[cache_key] = now
                    logger.debug("OKX 持仓模式已是 long_short_mode,无需切换")
                    return
    except Exception:
        pass  # 查询失败时继续尝试 set_position_mode

    try:
        if hasattr(ex, "set_position_mode"):
            await ex.set_position_mode(True, None, {"posMode": "long_short_mode"})
            _okx_mode_cache[cache_key] = now
            logger.info("OKX 持仓模式已确认/切换为 long_short_mode")
    except Exception as e:
        # S14修复: 失败也缓存，避免频繁重试和重复告警
        _okx_mode_cache[cache_key] = now
        logger.warning(f"OKX 持仓模式切换 long_short_mode 失败，将依赖 posSide 强约束: {e}")


async def load_exchange(
    db: AsyncSession,
    customer_id: int,
    exchange: str | None = None,
    testnet: bool | None = None,
    exchange_account_id: int | None = None,
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
    scalars = result.scalars()
    if hasattr(scalars, "first"):
        acc = scalars.first()
    else:
        items = scalars.all() if hasattr(scalars, "all") else list(scalars)
        acc = items[0] if items else None
    if not acc:
        raise ValueError(f"未配置 {exchange or exchange_account_id} 交易所账号")
    api_key = decrypt_secret(acc.api_key_enc)
    api_secret = decrypt_secret(acc.api_secret_enc)
    passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
    ex = _create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet, getattr(acc, "account_mode", "") or getattr(acc, "account_type", "") or "")
    try:
        await ex.load_markets()
        await _ensure_okx_long_short_mode(ex)
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




async def fetch_ohlcv(
    exchange: str, symbol: str, timeframe: str = "1h", limit: int = 50
) -> list[list]:
    """获取K线数据(公开行情,无需 API Key)。

    用于 ATR 计算等技术指标。
    返回格式: [[timestamp, open, high, low, close, volume], ...]
    """
    symbol = _normalize_symbol(exchange, symbol)
    ex = await _get_public_exchange(exchange)
    if not ex:
        return []
    await _rate_limit_wait(exchange)
    try:
        ohlcv = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return ohlcv or []
    except Exception as e:
        logger.warning(f"获取 {exchange} {symbol} K线失败: {e}")
        return []


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
        err_msg = str(e).lower()
        # 可忽略的错误:杠杆未修改/已设置/相同值
        if any(kw in err_msg for kw in ("not modified", "already", "same", "no need", "unchanged")):
            logger.debug(f"设置杠杆无需修改 {symbol} {leverage}x: {e}")
            return
        logger.error(f"设置杠杆失败 {symbol} {leverage}x: {type(e).__name__}")
        raise

def build_native_stop_loss_params(exchange: str, position_side: str, stop_price: float) -> dict[str, Any]:
    """构造交易所原生止损参数。"""
    ex_name = (exchange or "").lower()
    side = (position_side or "").lower()
    if ex_name == "okx":
        return {
            "tdMode": "cross",
            "posSide": side if side in ("long", "short") else "net",
            "slTriggerPx": str(float(stop_price)),
            "slOrdPx": "-1",
            "reduceOnly": True,
        }
    if ex_name == "binance":
        close_side = "SELL" if side == "long" else "BUY"
        return {
            "type": "STOP_MARKET",
            "side": close_side,
            "stopPrice": stop_price,
            "reduceOnly": True,
        }
    if ex_name == "bybit":
        return {
            "triggerPrice": stop_price,
            "reduceOnly": True,
        }
    raise ValueError(f"{exchange} 不支持原生止损")


async def place_native_stop_loss_order(
    ex,
    exchange: str,
    symbol: str,
    position_side: str,
    amount: float,
    stop_price: float,
) -> dict:
    """提交原生止损单；OKX/Binance/Bybit 走实盘提交，其他交易所显式拒绝避免误下单。"""
    ex_name = (exchange or getattr(ex, "id", "") or "").lower()
    close_side = "sell" if position_side == "long" else "buy"

    if ex_name == "okx":
        params = build_native_stop_loss_params("okx", position_side, stop_price)
        return await ex.create_order(
            symbol, "stop", close_side, amount, None,
            params={**params, "stopLossPrice": stop_price, "triggerPrice": stop_price}
        )
    elif ex_name == "binance":
        params = build_native_stop_loss_params("binance", position_side, stop_price)
        return await ex.create_order(
            symbol, "STOP_MARKET", close_side, amount, None,
            params=params
        )
    elif ex_name == "bybit":
        params = build_native_stop_loss_params("bybit", position_side, stop_price)
        return await ex.create_order(
            symbol, "market", close_side, amount, None,
            params={**params, "triggerPrice": stop_price}
        )
    else:
        raise ValueError(f"{exchange} 暂不支持原生止损实盘提交")




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

    # 防护: amount 必须大于 0,避免交易所拒绝或异常行为
    if not amount or amount <= 0:
        raise ValueError(f"下单数量必须大于 0,当前 amount={amount} symbol={symbol} side={side}")

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

    # BUG-9 修复: 精度调整后重新校验 amount > 0,避免小 amount 被截断为 0 导致下单异常
    if amount <= 0:
        raise ValueError(f"精度调整后数量为0,原始数量可能过小")

    params: dict[str, Any] = {}
    if reduce_only:
        # OKX 同样需要 reduceOnly(ccxt 会映射为 posSide/posSideMode 兼容参数),
        # 之前排除 OKX 会导致平仓单被当作开仓单,产生反向持仓
        params["reduceOnly"] = True
    # OKX 双向持仓模式(long_short_mode)必须指定 posSide；
    # 但净持仓模式(net_mode)传 posSide 会报 51000 Parameter posSide error。
    # 因此先按双向模式传，若交易所明确拒绝，再去掉 posSide 重试一次。
    if position_side and position_side in ("long", "short", "net"):
        if ex_name.lower() == "bybit":
            # Bybit 使用 positionIdx 区分持仓模式: 0=单向持仓,1=双向多,2=双向空。
            # 先按双向模式提交；若账户实际为单向模式，会在下方捕获 10001 后去掉该参数重试。
            if position_side == "long":
                params["positionIdx"] = 1
            elif position_side == "short":
                params["positionIdx"] = 2
            else:
                params["positionIdx"] = 0
        else:
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
            # 开仓单绝不能静默去掉 posSide 降级为 net_mode。
            # 本地仓位模型是 long/short 分仓；net_mode 会把反向开仓自动冲抵，
            # 造成交易所有净仓但本地分仓不匹配，最终出现孤儿仓/数量不一致。
            raise ValueError(
                "OKX 当前账户不是双向持仓模式，开仓被拒绝。"
                "请先将 OKX 持仓模式切换为 long_short_mode，或平掉现有净仓后由系统自动切换。"
            ) from e
        elif ex_name.lower() == "bybit" and ("position idx not match position mode" in msg or "10001" in msg) and "positionIdx" in params:
            fallback_params = dict(params)
            fallback_params.pop("positionIdx", None)
            logger.warning(
                f"Bybit 当前账户可能为单向持仓模式,positionIdx 被拒绝,去掉 positionIdx 重试: "
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
    """获取所有非零持仓。

    不吞异常: API 失败时向上抛出,由调用方决定如何处理。
    避免返回空列表导致调用方误判为"无持仓"。
    """
    positions = await ex.fetch_positions()
    return [p for p in positions if float(p.get("contracts", 0) or 0) > 0]


async def fetch_open_orders(ex) -> list[dict]:
    """获取所有未成交挂单(通过 exchange_adapter 统一调用)。

    与 fetch_positions 不同,此函数不吞异常,由调用方处理错误,
    避免API失败时将所有本地挂单误判为幽灵挂单。
    """
    orders = await ex.fetch_open_orders()
    return orders or []


def _extract_balance_from_ccxt(balance: dict) -> dict:
    info = balance.get("info", {})
    for key in ("totalEq", "totalWalletBalance", "totalMarginBalance"):
        if key in info and info[key]:
            try:
                # OM-M3修复: 同时返回 available_balance(可用保证金)
                usdt = balance.get("USDT", {})
                free = usdt.get("free", 0) or usdt.get("available", 0) or 0
                return {"equity": float(info[key]), "balance": float(info.get("totalWalletBalance", info[key])), "available_balance": float(free)}
            except (ValueError, TypeError):
                continue
    usdt = balance.get("USDT", {})
    total = usdt.get("total", 0) or 0
    free = usdt.get("free", 0) or usdt.get("available", 0) or 0
    return {"equity": total, "balance": total, "available_balance": float(free)}


def _extract_bybit_wallet_balance(data: dict) -> dict:
    """解析 Bybit V5 钱包余额,兼容 UNIFIED / CONTRACT。"""
    result = data.get("result") or {}
    accounts = result.get("list") or []
    if not accounts:
        return {"equity": 0.0, "balance": 0.0, "available_balance": 0.0, "unrealized_pnl": 0.0}

    account = accounts[0] or {}
    coins = account.get("coin") or []
    usdt = next((c for c in coins if str(c.get("coin", "")).upper() == "USDT"), {})

    def _num(*keys: str) -> float:
        for key in keys:
            value = usdt.get(key)
            if value in (None, ""):
                value = account.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    continue
        return 0.0

    equity = _num("equity", "totalEquity", "totalMarginBalance", "totalWalletBalance")
    wallet = _num("walletBalance", "totalWalletBalance", "totalMarginBalance", "totalEquity")
    # OM-M3修复: 提取可用余额(可用于下单的保证金)
    available = _num("availableToWithdraw", "availBalance", "maxWithdrawAmount")
    unrealized_pnl = _num("unrealisedPnl", "totalPerpUPL")
    return {"equity": equity or wallet, "balance": wallet or equity, "available_balance": available or wallet, "unrealized_pnl": unrealized_pnl}


async def _fetch_bybit_balance_native(ex) -> dict:
    """Bybit 测试网/UTA 下 ccxt 自动账户探测可能失败,优先直接读 V5 钱包余额。"""
    last_error: Exception | None = None
    for account_type in ("UNIFIED", "CONTRACT"):
        try:
            data = await ex.privateGetV5AccountWalletBalance({
                "accountType": account_type,
                "coin": "USDT",
            })
            bal = _extract_bybit_wallet_balance(data)
            if bal.get("equity", 0) or bal.get("balance", 0):
                return bal
        except Exception as e:
            last_error = e
            logger.debug(f"Bybit {account_type} 余额查询失败: {e}")
    if last_error:
        raise last_error
    return {"equity": 0.0, "balance": 0.0, "available_balance": 0.0, "unrealized_pnl": 0.0}


async def fetch_balance(ex) -> dict:
    if (getattr(ex, "id", "") or "").lower() == "bybit":
        try:
            return await _fetch_bybit_balance_native(ex)
        except Exception as e:
            logger.warning(f"Bybit 原生余额查询失败,回退 ccxt fetch_balance: {e}")
    balance = await ex.fetch_balance()
    return _extract_balance_from_ccxt(balance)


async def close_position_market(ex, symbol: str, side: str, amount: float) -> dict:
    """市价平仓。

    Args:
        side: 持仓方向('long'/'short'),用于确定平仓订单方向和 posSide。
        amount: 持仓币数(ETH/BTC 等),会自动转为合约数。
    """
    # M-5修复: 校验平仓数量必须大于 0
    if amount <= 0:
        raise ValueError(f"平仓数量必须大于 0, 当前: {amount}")
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
                    pos_symbol = pos.get("symbol") or (pos.get("info") or {}).get("instId")
                    pos_side = (pos.get("info") or {}).get("posSide")
                    contracts = abs(float(pos.get("contracts") or (pos.get("info") or {}).get("pos") or 0))
                    if (pos_symbol == norm_symbol or pos_symbol == symbol) and contracts > 0 and pos_side == "net":
                        position_side = "net"
                        break
            except Exception as e:
                logger.debug(f"探测 OKX 持仓模式失败,按方向 posSide 平仓: {e}")
    except Exception as e:
        # BUG-7 修复: 不再静默吞掉异常。market 获取失败时 cs 未定义,
        # amount/cs 转换不会执行(异常已跳过),amount 保持原值即按 cs=1.0 处理。
        logger.warning(f"获取market信息失败,跳过合约数转换(按 cs=1.0 处理): {e}")
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


async def close_exchange(ex) -> None:
    try:
        await ex.close()
    except Exception:
        pass

async def close_all_public_exchanges() -> None:
    """S11新增: 关闭所有缓存的公开行情交易所实例,防止HTTP连接池泄漏。"""
    global _public_exchanges
    if not _public_exchanges:
        return
    count = 0
    for key, ex in list(_public_exchanges.items()):
        try:
            await ex.close()
            count += 1
        except Exception:
            pass
    _public_exchanges.clear()
    if count > 0:
        logger.info(f"已关闭 {count} 个公开行情交易所实例")



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
                    # OM-M3修复: 从 OKX 响应中提取可用余额
                    avail = 0.0
                    for d in account.get("details", []):
                        if str(d.get("ccy", "")).upper() == "USDT":
                            avail = float(d.get("availBal") or d.get("cashBal") or 0)
                            break
                    return {"equity": total_eq or total_wb, "balance": total_wb, "available_balance": avail or total_wb}
            # BUG-5 修复: API 错误时 raise 异常而非返回 0,调用方可区分"余额为0"和"查询失败"
            raise RuntimeError(f"OKX 原生余额查询失败: {data.get('msg', 'unknown')}")
    except RuntimeError:
        raise
    except Exception as e:
        # BUG-5 修复: 网络/解析异常也向上抛出,不再吞掉返回 0
        raise RuntimeError(f"OKX 原生余额查询异常: {e}") from e


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
            return {"equity": 0.0, "balance": 0.0, "available_balance": 0.0}
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
            # BUG-5 修复: API 错误时 raise 异常而非返回空列表,调用方可区分"无持仓"和"查询失败"
            raise RuntimeError(f"OKX 原生持仓查询失败: {data.get('msg', 'unknown')}")
    except RuntimeError:
        raise
    except Exception as e:
        # BUG-5 修复: 网络/解析异常也向上抛出,不再吞掉返回空列表
        raise RuntimeError(f"OKX 原生持仓查询异常: {e}") from e
