"""LLM 信号解析器 - 使用大模型解析复杂/非结构化交易信号。

双 LLM 架构:
  - 文本信号:走 text_llm(默认 DeepSeek V3)
  - 图片信号:走 vision_llm(默认 GLM-4V,仅对 KOL.vision_llm_enabled=True 生效)

增强功能(借鉴 KOL 跟单系统):
  - URL 剥离:发送 LLM 前移除 URL,避免内容审核 400 错误
  - 纯 URL 预过滤:纯 URL 消息直接跳过 LLM,节省 token 开销
  - 模型降级:主模型超时/异常时自动切换 deepseek-v4-pro,当天降级,次日重置
  - 历史上下文注入:支持传入 KOL 历史信号上下文,提升解析准确率
  - 重试机制:LLM 调用失败时自动重试 3 次,指数退避(1s, 2s, 4s)
  - 超时保护:备用模型调用添加 15 秒超时保护
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from loguru import logger

from app.schemas.signal import ParsedSignal
from app.services.llm_client import LLMClient, get_text_llm_client, get_vision_llm_client


# ---------------------------------------------------------------------------
# URL 处理
# ---------------------------------------------------------------------------

# 匹配文本中的 URL(用于剥离,避免内容审核 400 错误)
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

# 匹配纯 URL 消息(整条消息仅由一个或多个 URL 组成,允许首尾空白)
_PURE_URL_RE = re.compile(r'^\s*(https?://\S+\s*)+$', re.IGNORECASE)


def _strip_urls(text: str) -> str:
    """移除文本中的所有 URL。

    某些 LLM 提供商(如 DeepSeek)的内容审核会对包含外部 URL 的输入返回 400 错误,
    在发送前剥离 URL 可避免此问题。

    Args:
        text: 原始文本

    Returns:
        剥离 URL 后的文本
    """
    return _URL_RE.sub('', text)


# ---------------------------------------------------------------------------
# 模型降级机制
# ---------------------------------------------------------------------------

# 备用模型名称：借鉴朋友服务器配置，主模型不稳/超时时切到更强的 V4-Pro。
_FALLBACK_MODEL = "deepseek-v4-pro"

# 主模型超时时间(秒,与 KOL 跟单系统一致)
_PRIMARY_TIMEOUT = 15

# ★ P1 修复: 备用模型超时时间(秒)
_FALLBACK_TIMEOUT = 15

# ★ P1 修复: LLM 调用重试次数
_MAX_RETRIES = 2

# ★ P1 修复: 指数退避延迟(秒): 1, 2, 4
_RETRY_DELAYS = [2, 4]

# 全局降级状态:当天主模型失败后标记降级,后续直接走备用模型
_model_degraded = False
_degraded_date: str | None = None


def _check_reset_degradation() -> None:
    """跨天时重置模型降级状态。

    降级仅当天有效,次日自动恢复使用主模型。
    在每次 parse_with_llm() 调用开始时执行。
    """
    global _model_degraded, _degraded_date
    today = time.strftime("%Y-%m-%d")
    if _model_degraded and _degraded_date != today:
        logger.info("跨天重置模型降级状态,恢复使用主模型")
        _model_degraded = False
        _degraded_date = None


def _mark_degraded() -> None:
    """标记主模型降级,当天剩余时间均使用备用模型。"""
    global _model_degraded, _degraded_date
    _model_degraded = True
    _degraded_date = time.strftime("%Y-%m-%d")
    logger.warning(f"主模型已降级,当天将使用备用模型: {_FALLBACK_MODEL}")


def _get_fallback_client(primary: LLMClient) -> LLMClient:
    """基于主客户端配置构造备用客户端(使用 deepseek-v4-pro 模型)。

    复用主客户端的 provider/api_key/api_base 等连接配置,仅替换模型名称。
    """
    return LLMClient(
        provider=primary.provider,
        api_key=primary.api_key,
        model=_FALLBACK_MODEL,
        api_base=primary.api_base,
        temperature=primary.temperature,
        max_tokens=primary.max_tokens,
        timeout=primary.timeout,
        enabled=primary._enabled_override,
    )


async def _get_glm_fallback_client() -> LLMClient | None:
    """GLM fallback client using vision LLM SiliconFlow API key."""
    try:
        from app.core.runtime_config import get_vision_llm_settings
        vision_cfg = await get_vision_llm_settings()
        if vision_cfg.api_key and vision_cfg.provider != "deepseek":
            logger.info(
                f"GLM fallback: provider={vision_cfg.provider}, "
                f"model={vision_cfg.model or 'GLM-4.5V'}"
            )
            return LLMClient(
                provider=vision_cfg.provider,
                api_key=vision_cfg.api_key,
                model=vision_cfg.model or "zai-org/GLM-4.5V",
                api_base=vision_cfg.api_base,
                temperature=0.1,
                max_tokens=2000,
                timeout=_FALLBACK_TIMEOUT,
                enabled=True,
            )
    except Exception as e:
        logger.warning(f"GLM fallback client failed: {e}")
    return None


async def _call_llm_with_retry(
    client: LLMClient,
    text: str,
    timeout: int | None = None,
    max_retries: int = _MAX_RETRIES,
    retry_delays: list[int] | None = None,
) -> dict[str, Any]:
    """带重试机制的 LLM 调用。

    ★ P1 修复: 添加 3 次重试,指数退避(1s, 2s, 4s)。

    Args:
        client: LLM 客户端实例
        text: 已处理的文本
        timeout: 调用超时时间(秒),None 表示不设超时
        max_retries: 最大重试次数
        retry_delays: 重试间隔列表(秒),如 [1, 2, 4]

    Returns:
        LLM 分析结果字典

    Raises:
        Exception: 所有重试均失败后,最后一个异常向上传播
    """
    if retry_delays is None:
        retry_delays = _RETRY_DELAYS

    last_exception: Exception | None = None
    total_attempts = max_retries + 1  # 首次调用 + 重试次数

    for attempt in range(total_attempts):
        try:
            if timeout is not None:
                # ★ P1 修复: 使用 asyncio.wait_for 添加超时保护
                result = await asyncio.wait_for(
                    client.analyze_signal(text),
                    timeout=timeout,
                )
            else:
                result = await client.analyze_signal(text)
            return result
        except asyncio.TimeoutError:
            last_exception = asyncio.TimeoutError()
            if attempt < total_attempts - 1:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1] * 2
                logger.warning(
                    f"LLM 调用超时(attempt {attempt + 1}/{total_attempts}), "
                    f"{delay}s 后重试..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"LLM 调用在 {total_attempts} 次尝试后仍超时")
        except Exception as e:
            last_exception = e
            if attempt < total_attempts - 1:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1] * 2
                logger.warning(
                    f"LLM 调用异常(attempt {attempt + 1}/{total_attempts}): {e}, "
                    f"{delay}s 后重试..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"LLM 调用在 {total_attempts} 次尝试后仍失败: {e}")

    raise last_exception if last_exception else Exception("LLM 调用失败,未知原因")


async def _call_with_degradation(
    primary_client: LLMClient,
    text: str,
) -> dict[str, Any]:
    """Three-tier LLM degradation."""
    # Tier 1: primary model (deepseek-v4-flash, thinking disabled)
    if not _model_degraded:
        try:
            return await _call_llm_with_retry(
                primary_client, text,
                timeout=_PRIMARY_TIMEOUT,
                max_retries=_MAX_RETRIES,
                retry_delays=_RETRY_DELAYS,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(
                f"primary model failed (retried {_MAX_RETRIES}x): {e}, "
                f"switching to fallback and marking degraded"
            )
            _mark_degraded()

    # Tier 2: same-provider fallback (deepseek-chat, non-thinking)
    try:
        fallback = _get_fallback_client(primary_client)
        logger.info(f"using fallback model: {_FALLBACK_MODEL}")
        return await _call_llm_with_retry(
            fallback, text,
            timeout=_FALLBACK_TIMEOUT,
            max_retries=_MAX_RETRIES,
            retry_delays=_RETRY_DELAYS,
        )
    except Exception as e:
        logger.warning(f"fallback model failed: {e}, trying GLM disaster recovery")

    # Tier 3: GLM disaster recovery (different provider)
    glm_client = await _get_glm_fallback_client()
    if glm_client:
        return await _call_llm_with_retry(
            glm_client, text,
            timeout=_FALLBACK_TIMEOUT,
            max_retries=_MAX_RETRIES,
            retry_delays=_RETRY_DELAYS,
        )

    raise Exception("All LLM models (primary/fallback/GLM) unavailable")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _lazy_normalize_symbol(raw: str) -> str:
    """延迟导入 normalize_symbol,避免 signal_parser 循环导入。"""
    from app.services.signal_parser import normalize_symbol
    return normalize_symbol(raw)




def _as_float(value: Any, default: float = 0.0) -> float:
    """安全转换 LLM 返回的数字字段,避免字符串/None/异常类型导致解析崩溃。"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_float(value: Any) -> float | None:
    """安全转换可选价格字段。"""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float_list(value: Any) -> list[float]:
    """安全转换 LLM 返回的止盈列表,过滤 None/字符串垃圾值。"""
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        value = [value]
    out: list[float] = []
    for item in value:
        number = _as_optional_float(item)
        if number is not None:
            out.append(number)
    return out


def _as_bool(value: Any) -> bool:
    """安全转换 LLM 返回的布尔字段。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "是", "有效", "valid")
    return False

def _normalize_side(raw: Any) -> str:
    """将 LLM 返回的方向统一为 long/short。

    LLM 可能返回 "LONG"/"Long"/"buy"/"多"/"多单"/"bullish" 等各种格式,
    下游(下单/持仓/风控)统一使用小写 long/short,不归一化会导致方向不匹配。
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    if s in ("long", "buy", "多", "多单", "做多", "开多", "看多", "bull", "bullish", "long_only", "买入"):
        return "long"
    if s in ("short", "sell", "空", "空单", "做空", "开空", "看空", "bear", "bearish", "short_only", "卖出"):
        return "short"
    # 兜底:包含关键字符则归类
    if "多" in s or "buy" in s or "long" in s or "bull" in s:
        return "long"
    if "空" in s or "sell" in s or "short" in s or "bear" in s:
        return "short"
    return ""  # 无法识别,返回空(后续过滤会拒绝无方向信号)


# ---------------------------------------------------------------------------
# 文本信号解析
# ---------------------------------------------------------------------------

async def parse_with_llm(
    text: str,
    image_urls: list[str] | None = None,
    image_base64_list: list[str] | None = None,
    llm_client: LLMClient | None = None,
    context: str = "",
) -> tuple[ParsedSignal | None, dict[str, Any]]:
    """
    使用文本 LLM 解析交易信号(DeepSeek V3)。

    这是规则解析失败后的兜底方案，用于处理非结构化文本或复杂场景。
    仅处理文本,不传图片(图片走 parse_image_with_llm)。

    增强功能:
      - 纯 URL 消息预过滤,直接跳过 LLM
      - 发送前剥离 URL,避免内容审核 400 错误
      - 模型降级:主模型超时/异常时自动切换 deepseek-v4-pro
      - 历史上下文注入:传入 KOL 历史信号,提升解析准确率
      - 重试机制:LLM 调用失败时自动重试 3 次,指数退避(1s, 2s, 4s)
      - 超时保护:主模型 10s 超时,备用模型 15s 超时

    Args:
        text: 信号文本
        image_urls: 图片 URL 列表(忽略,文本 LLM 不处理图片)
        image_base64_list: 图片 base64 列表(忽略)
        llm_client: 文本 LLM 客户端实例(可选)
        context: KOL 历史信号上下文(可选),格式示例:
            [该KOL历史信号]
            ① open_long BTC/USDT 进场64000 止损62000
            ② close_all BTC/USDT
            ③ open_short ETH/USDT 进场3200

    Returns:
        (ParsedSignal 对象, usage 信息)，如果解析失败返回 (None, {})
    """
    # 1. 跨天重置降级状态(每次调用开始时检查)
    _check_reset_degradation()

    # 2. 纯 URL 预过滤:整条消息仅由 URL 组成时跳过 LLM
    if _PURE_URL_RE.match(text or ""):
        logger.info("消息为纯 URL,跳过 LLM 解析")
        return None, {}

    # 3. 剥离 URL(避免内容审核 400 错误)
    stripped_text = _strip_urls(text or "")

    # 4. 注入历史上下文(如有)
    if context:
        final_text = f"{context}\n[当前消息]\n{stripped_text}"
    else:
        final_text = stripped_text

    # 5. 获取主客户端(强制走文本 LLM,忽略图片参数)
    primary_client = llm_client or await get_text_llm_client()

    if not primary_client.is_available:
        logger.warning("文本 LLM 未启用，跳过 LLM 解析")
        return None, {}

    try:
        # 6. 带降级机制的 LLM 调用(含重试和超时保护)
        #    - 未降级:主模型(10s 超时 + 3次重试) -> 失败则备用模型(15s 超时 + 3次重试)
        #    - 已降级:直接备用模型 deepseek-v4-pro
        response = await _call_with_degradation(primary_client, final_text)
        result = response.get("result", {})
        usage = response.get("usage", {})

        # 检查是否为有效信号
        if not _as_bool(result.get("is_valid_signal", False)):
            logger.info(f"文本 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text=text,
                confidence=0.0,
            ), usage

        # 构建 ParsedSignal
        is_exit = _as_bool(result.get("is_exit_signal", False))
        position_pct = _as_float(result.get("position_pct"), 0.0)
        if not is_exit and position_pct <= 0:
            # LLM 有时不会返回仓位比例，使用规则解析兜底识别:
            # "半仓"=50%, "三成仓"=30%, "轻仓"=30%, "重仓"=70%。
            from app.services.signal_parser import extract_position_pct
            position_pct = extract_position_pct(stripped_text)
        parsed = ParsedSignal(
            symbol=_lazy_normalize_symbol(result.get("symbol", "")),  # 统一为 BTC/USDT 格式
            side=_normalize_side(result.get("side", "")) if not is_exit else "",  # 平仓信号不带方向(平掉该品种所有方向持仓)
            entry_price=None if is_exit else _as_optional_float(result.get("entry_price")),
            entry_prices=[] if is_exit else _as_float_list(result.get("entry_prices", [])),
            take_profits=[] if is_exit else _as_float_list(result.get("take_profits", [])),
            condition_price=None if is_exit else _as_optional_float(result.get("condition_price")),
            breakeven_after_tp=None if is_exit else _as_optional_float(result.get("breakeven_after_tp")),
            stop_loss=None if is_exit else _as_optional_float(result.get("stop_loss")),
            position_pct=0.0 if is_exit else max(0.0, min(position_pct, 100.0)),
            raw_text=text,
            confidence=max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0)),
            is_exit_signal=is_exit,
            exit_reason=result.get("reasoning", ""),
            has_image=False,
        )

        if not parsed.entry_price and parsed.entry_prices:
            parsed.entry_price = parsed.entry_prices[0]

        # 如果没有品种信息，尝试从文本提取
        if not parsed.symbol:
            parsed.symbol = _extract_symbol_fallback(text)

        # 借鉴朋友服务器的解析保护: 用止盈/入场/止损的价格关系自动纠正多空方向。
        # 正常多单通常是 TP > Entry > SL；正常空单通常是 TP < Entry < SL。
        # 只在三者都明确且关系非常清晰时纠正，避免覆盖无止损/无止盈的信号。
        if (
            not parsed.is_exit_signal
            and parsed.side in ("long", "short")
            and parsed.entry_price is not None
            and parsed.stop_loss is not None
            and parsed.take_profits
        ):
            tp_price = parsed.take_profits[0]
            ep = parsed.entry_price
            sl = parsed.stop_loss
            if tp_price > ep > sl and parsed.side == "short":
                logger.warning(
                    f"LLM 方向纠正: short→long (TP={tp_price}>EP={ep}>SL={sl})"
                )
                parsed.side = "long"
            elif tp_price < ep < sl and parsed.side == "long":
                logger.warning(
                    f"LLM 方向纠正: long→short (TP={tp_price}<EP={ep}<SL={sl})"
                )
                parsed.side = "short"

        logger.info(
            f"文本 LLM 解析成功: symbol={parsed.symbol}, side={parsed.side}, "
            f"is_exit={parsed.is_exit_signal}, confidence={parsed.confidence}, "
            f"tokens={usage.get('total_tokens', 0)}"
        )

        return parsed, usage

    except Exception as e:
        logger.error(f"文本 LLM 解析异常: {e}")
        return None, {}


# ---------------------------------------------------------------------------
# 兜底品种提取
# ---------------------------------------------------------------------------

def _extract_symbol_fallback(text: str) -> str:
    """当 LLM 未能提取品种时，使用简单规则作为兜底。返回统一 BTC/USDT 格式。"""
    # 常见币种列表
    common_symbols = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC",
        "LINK", "LTC", "AVAX", "UNI", "ATOM", "ETC", "BCH", "FIL", "APT",
        "ARBITRUM", "OP", "DYDX", "AAVE", "MKR", "COMP", "CRV", "SNX",
        "INJ", "SUI", "TIA", "SEI", "ORDI", "PEPE", "WIF", "FLOKI",
        "JUP", "WEN", "BONK", "JTO", "TURBO", "MEW",
    ]

    # 尝试匹配 $TICKER 格式
    m = re.search(r"\$([A-Za-z0-9]{2,10})", text)
    if m:
        return _lazy_normalize_symbol(m.group(1))

    # 尝试匹配常见币种
    text_upper = text.upper()
    for symbol in common_symbols:
        # 确保匹配完整单词
        if re.search(rf"\b{symbol}\b", text_upper):
            return _lazy_normalize_symbol(symbol)

    # 尝试匹配 TICKER/USDT 格式
    m = re.search(r"\b([A-Z]{2,10})\s*/\s*USDT\b", text_upper)
    if m:
        return _lazy_normalize_symbol(m.group(1))

    return ""


# ---------------------------------------------------------------------------
# 图片信号解析
# ---------------------------------------------------------------------------

async def parse_image_with_llm(
    image_urls: list[str] | None = None,
    image_base64_list: list[str] | None = None,
    llm_client: LLMClient | None = None,
    kol_vision_enabled: bool = False,
) -> tuple[ParsedSignal | None, dict[str, Any]]:
    """
    直接使用图片 LLM 分析图片(GLM-4V,不经过 OCR)。

    仅当 KOL.vision_llm_enabled=True 且全局 vision_llm 已配置时才调用。
    适用于支持多模态的模型。

    Args:
        image_urls: 图片 URL 列表
        image_base64_list: 图片 base64 列表
        llm_client: 图片 LLM 客户端实例(可选,不传则从 runtime_config 构造)
        kol_vision_enabled: 该 KOL 是否启用图片 LLM(由调用方传入)

    Returns:
        (ParsedSignal 对象, usage 信息)
    """
    # KOL 未启用图片 LLM,直接返回
    if not kol_vision_enabled:
        logger.debug("KOL 未启用图片 LLM,跳过")
        return None, {}

    # 用 vision LLM 客户端
    client = llm_client or await get_vision_llm_client()

    if not client.is_available:
        logger.warning("图片 LLM 未启用或未配置,跳过图片解析")
        return None, {}

    text = "请分析这些图片中的加密货币交易策略，提取品种、方向、入场价、止盈、止损等信息。"

    try:
        # 直接调 vision client 的 analyze_signal(带图片)
        # ★ P1 修复: 图片 LLM 也使用重试机制和超时保护
        response = await _call_llm_with_retry(
            client, text,
            timeout=_FALLBACK_TIMEOUT,
            max_retries=_MAX_RETRIES,
            retry_delays=_RETRY_DELAYS,
        )
        result = response.get("result", {})
        usage = response.get("usage", {})

        if not _as_bool(result.get("is_valid_signal", False)):
            logger.info(f"图片 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text=text,
                confidence=0.0,
                has_image=True,
            ), usage

        position_pct = _as_float(result.get("position_pct"), 0.0)
        parsed = ParsedSignal(
            symbol=_lazy_normalize_symbol(result.get("symbol", "")),  # 统一为 BTC/USDT 格式
            side=_normalize_side(result.get("side", "")),  # 统一为 long/short
            entry_price=_as_optional_float(result.get("entry_price")),
            entry_prices=_as_float_list(result.get("entry_prices", [])),
            take_profits=_as_float_list(result.get("take_profits", [])),
            condition_price=_as_optional_float(result.get("condition_price")),
            breakeven_after_tp=_as_optional_float(result.get("breakeven_after_tp")),
            stop_loss=_as_optional_float(result.get("stop_loss")),
            position_pct=max(0.0, min(position_pct, 100.0)),
            raw_text=text,
            confidence=max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0)),
            is_exit_signal=_as_bool(result.get("is_exit_signal", False)),
            exit_reason=result.get("reasoning", ""),
            has_image=True,
        )

        logger.info(
            f"图片 LLM 解析成功: symbol={parsed.symbol}, side={parsed.side}, "
            f"confidence={parsed.confidence}, tokens={usage.get('total_tokens', 0)}"
        )
        return parsed, usage

    except Exception as e:
        logger.error(f"图片 LLM 解析异常: {e}")
        return None, {}
