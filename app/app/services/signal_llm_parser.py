"""LLM 信号解析器 - 使用大模型解析复杂/非结构化交易信号。

双 LLM 架构:
  - 文本信号:走 text_llm(默认 DeepSeek V3)
  - 图片信号:走 vision_llm(默认 GLM-4V,仅对 KOL.vision_llm_enabled=True 生效)
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from app.schemas.signal import ParsedSignal
from app.services.llm_client import LLMClient, get_text_llm_client, get_vision_llm_client


async def parse_with_llm(
    text: str,
    image_urls: list[str] | None = None,
    image_base64_list: list[str] | None = None,
    llm_client: LLMClient | None = None,
) -> tuple[ParsedSignal | None, dict[str, Any]]:
    """
    使用文本 LLM 解析交易信号(DeepSeek V3)。

    这是规则解析失败后的兜底方案，用于处理非结构化文本或复杂场景。
    仅处理文本,不传图片(图片走 parse_image_with_llm)。

    Args:
        text: 信号文本
        image_urls: 图片 URL 列表(忽略,文本 LLM 不处理图片)
        image_base64_list: 图片 base64 列表(忽略)
        llm_client: 文本 LLM 客户端实例(可选)

    Returns:
        (ParsedSignal 对象, usage 信息)，如果解析失败返回 (None, {})
    """
    # 强制走文本 LLM(忽略图片参数)
    client = llm_client or await get_text_llm_client()

    if not client.is_available:
        logger.warning("文本 LLM 未启用，跳过 LLM 解析")
        return None, {}

    try:
        # 仅传文本,不带图片
        response = await client.analyze_signal(text)
        result = response.get("result", {})
        usage = response.get("usage", {})

        # 检查是否为有效信号
        if not result.get("is_valid_signal", False):
            logger.info(f"文本 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text=text,
                confidence=0.0,
            ), usage

        # 构建 ParsedSignal
        parsed = ParsedSignal(
            symbol=result.get("symbol", "").replace("/", ""),  # BTC/USDT -> BTCUSDT
            side=result.get("side", ""),
            entry_price=result.get("entry_price"),
            take_profits=result.get("take_profits", []),
            stop_loss=result.get("stop_loss"),
            raw_text=text,
            confidence=result.get("confidence", 0.5),
            is_exit_signal=result.get("is_exit_signal", False),
            exit_reason=result.get("reasoning", ""),
            has_image=False,
        )

        # 如果没有品种信息，尝试从文本提取
        if not parsed.symbol:
            parsed.symbol = _extract_symbol_fallback(text)

        logger.info(
            f"文本 LLM 解析成功: symbol={parsed.symbol}, side={parsed.side}, "
            f"is_exit={parsed.is_exit_signal}, confidence={parsed.confidence}, "
            f"tokens={usage.get('total_tokens', 0)}"
        )

        return parsed, usage

    except Exception as e:
        logger.error(f"文本 LLM 解析异常: {e}")
        return None, {}


def _extract_symbol_fallback(text: str) -> str:
    """当 LLM 未能提取品种时，使用简单规则作为兜底。"""
    import re

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
        return m.group(1).upper() + "USDT"

    # 尝试匹配常见币种
    text_upper = text.upper()
    for symbol in common_symbols:
        # 确保匹配完整单词
        if re.search(rf"\b{symbol}\b", text_upper):
            return symbol + "USDT"

    # 尝试匹配 TICKER/USDT 格式
    m = re.search(r"\b([A-Z]{2,10})\s*/\s*USDT\b", text_upper)
    if m:
        return m.group(1) + "USDT"

    return ""


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
        response = await client.analyze_signal(text, image_urls, image_base64_list)
        result = response.get("result", {})
        usage = response.get("usage", {})

        if not result.get("is_valid_signal", False):
            logger.info(f"图片 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text=text,
                confidence=0.0,
                has_image=True,
            ), usage

        parsed = ParsedSignal(
            symbol=result.get("symbol", "").replace("/", ""),
            side=result.get("side", ""),
            entry_price=result.get("entry_price"),
            take_profits=result.get("take_profits", []),
            stop_loss=result.get("stop_loss"),
            raw_text=text,
            confidence=result.get("confidence", 0.5),
            is_exit_signal=result.get("is_exit_signal", False),
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