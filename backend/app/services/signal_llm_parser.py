"""LLM 信号解析器 - 使用大模型解析复杂/非结构化交易信号。

双 LLM 架构:
  - 文本信号:走 text_llm(默认 DeepSeek V3)
  - 图片信号:走 vision_llm(默认 GLM-4V,仅对 KOL.vision_llm_enabled=True 生效)

增强功能(借鉴 KOL 跟单系统):
  - URL 剥离:发送 LLM 前移除 URL,避免内容审核 400 错误
  - 纯 URL 预过滤:纯 URL 消息直接跳过 LLM,节省 token 开销
  - 模型降级:主模型超时/异常时自动切换 deepseek-v4-pro,当天降级,次日重置
  - 历史上下文注入:支持传入 KOL 历史信号上下文,提升解析准确率
  - 重试机制:LLM 调用失败时最多尝试 3 次,指数退避(2s, 4s)
  - 超时保护:备用模型调用添加 15 秒超时保护
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
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

# ---------------------------------------------------------------------------
# LLM 解析结果缓存 (Redis)
# ---------------------------------------------------------------------------
import hashlib as _hashlib
from app.core.redis import get_redis as _get_redis

_LLM_CACHE_TTL = 3600  # 缓存 1 小时


def _llm_cache_key(text: str) -> str:
    """从信号文本生成 Redis 缓存键。"""
    normalized = (text or "").strip().lower()[:500]
    text_hash = _hashlib.md5(normalized.encode()).hexdigest()
    return f"dcq:llm_parse:{text_hash}"


async def _get_cached_llm_result(text: str) -> dict | None:
    """从 Redis 获取缓存的 LLM 解析结果。"""
    try:
        redis = await _get_redis()
        if redis is None:
            return None
        cached = await redis.get(_llm_cache_key(text))
        if cached:
            import json as _json
            return _json.loads(cached)
    except Exception as e:
        logger.debug(f"LLM cache get failed: {e}")
    return None


async def _set_cached_llm_result(text: str, result: dict, usage: dict) -> None:
    """将 LLM 解析结果存入 Redis。"""
    try:
        redis = await _get_redis()
        if redis is None:
            return
        import json as _json
        payload = _json.dumps({"result": result, "usage": usage}, ensure_ascii=False)
        await redis.setex(_llm_cache_key(text), _LLM_CACHE_TTL, payload)
        logger.debug(f"LLM 结果已缓存, TTL={_LLM_CACHE_TTL}s")
    except Exception as e:
        logger.debug(f"LLM cache set failed: {e}")

# 模型降级机制
# ---------------------------------------------------------------------------

# 备用模型名称：借鉴朋友服务器配置，主模型不稳/超时时切到更强的 V4-Pro。
_FALLBACK_MODEL = "deepseek-v4-pro"

# 主模型超时时间(秒,与 KOL 跟单系统一致)
_PRIMARY_TIMEOUT = 8

# ★ P1 修复: 备用模型超时时间(秒)
_FALLBACK_TIMEOUT = 10

# ★ P1 修复: LLM 调用重试次数(2 次重试 = 最多 3 次尝试)
_MAX_RETRIES = 1

# ★ P1 修复: 指数退避延迟(秒): 2, 4
_RETRY_DELAYS = [1, 2]

# 低置信度重试：模型正常返回但 confidence 低于执行阈值时，重新解析几次。
# 这类重试不同于网络/超时重试，目的是给模型第二次理解机会，减少误拦截。
_LOW_CONFIDENCE_RETRY_COUNT = 1
_DEFAULT_MIN_CONFIDENCE = 0.5

# 全局降级状态:当天主模型失败后标记降级,后续直接走备用模型
_model_degraded = False
_degraded_date: str | None = None


def _check_reset_degradation() -> None:
    """跨天时重置模型降级状态。

    降级仅当天有效,次日自动恢复使用主模型。
    在每次 parse_with_llm() 调用开始时执行。
    """
    global _model_degraded, _degraded_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _model_degraded and _degraded_date != today:
        logger.info("跨天重置模型降级状态,恢复使用主模型")
        _model_degraded = False
        _degraded_date = None


def _mark_degraded() -> None:
    """标记主模型降级,当天剩余时间均使用备用模型。"""
    global _model_degraded, _degraded_date
    _model_degraded = True
    _degraded_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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


# ---------------------------------------------------------------------------
# LLM 欠费(402)检测与充值提醒告警
# ---------------------------------------------------------------------------
_LLM_PAYMENT_ALERT_COOLDOWN = 1800  # 冷却30分钟, 欠费期间避免每条信号都刷告警
_LLM_PAYMENT_ERROR_PATTERNS = (
    "402",                     # HTTP 402 Payment Required
    "payment required",
    "insufficient balance",    # DeepSeek 官方返回体
    "balance is not enough",
    "arrearage",               # 部分供应商用词
)
_llm_payment_last_alert_ts: float = 0.0  # 内存兜底冷却(Redis 不可用时)


def _is_llm_payment_error(e: BaseException) -> bool:
    """判断 LLM 调用异常是否为账户欠费(402/余额不足)类错误。"""
    msg = str(e).lower()
    return any(p in msg for p in _LLM_PAYMENT_ERROR_PATTERNS)


async def _alert_llm_payment_error(e: BaseException) -> None:
    """LLM 账户欠费时发送充值提醒告警(30分钟冷却,Redis 跨进程+内存兜底)。"""
    global _llm_payment_last_alert_ts
    now = time.time()
    redis = None
    try:
        redis = await _get_redis()
    except Exception:
        redis = None
    if redis is not None:
        try:
            # SET NX: 冷却期内已告警过则返回 None,不再发送
            sent = await redis.set(
                "llm:payment_alert_cooldown",
                "1",
                ex=_LLM_PAYMENT_ALERT_COOLDOWN,
                nx=True,
            )
            if not sent:
                return
        except Exception as cd_e:
            logger.debug(f"[LLM欠费] Redis 冷却检查失败,降级内存冷却: {cd_e}")
            redis = None
    if redis is None and now - _llm_payment_last_alert_ts < _LLM_PAYMENT_ALERT_COOLDOWN:
        return
    _llm_payment_last_alert_ts = now
    try:
        from sqlalchemy import select as _sa_select

        from app.core.database import AsyncSessionLocal as _SessionLocal
        from app.models.config import AlertConfig as _AlertConfig
        from app.services.notification import notify

        # notify(None) 仅路由全局配置,若全局配置被禁用会静默丢失;
        # 系统级故障应发给所有启用了 on_error 告警的订阅者(含全局)
        target_customers: list[int | None] = [None]
        try:
            async with _SessionLocal() as db:
                cfgs = (
                    await db.execute(
                        _sa_select(_AlertConfig.customer_id).where(
                            _AlertConfig.enabled.is_(True),
                            _AlertConfig.on_error.is_(True),
                        )
                    )
                ).scalars().all()
                seen: set[int | None] = {None}
                for cid in cfgs:
                    if cid is not None and cid not in seen:
                        seen.add(cid)
                        target_customers.append(cid)
        except Exception as q_e:
            logger.debug(f"[LLM欠费] 告警目标查询失败,按全局发送: {q_e}")

        for _cid in target_customers:
            await notify(
                "error",
                "LLM API 账户欠费,请充值",
                f"错误信息: {str(e)[:300]}\n"
                f"影响: 新信号将降级为 OCR/规则模式解析,解析质量下降\n"
                f"建议: 请尽快登录 LLM 服务商(DeepSeek)控制台充值\n"
                f"说明: 欠费期间每 30 分钟提醒一次,充值后自动恢复",
                _cid,
            )
        logger.warning(f"[LLM欠费] 已发送充值提醒告警: {str(e)[:200]}")
    except Exception as notify_e:
        logger.warning(f"[LLM欠费] 告警发送失败: {notify_e}")


async def _call_llm_with_retry(
    client: LLMClient,
    text: str,
    image_urls: list[str] | None = None,
    image_base64_list: list[str] | None = None,
    timeout: int | None = None,
    max_retries: int = _MAX_RETRIES,
    retry_delays: list[int] | None = None,
    kol_name: str = "",
) -> dict[str, Any]:
    """带重试机制的 LLM 调用。

    ★ P1 修复: 添加失败重试,指数退避(2s, 4s)。

    Args:
        client: LLM 客户端实例
        text: 已处理的文本
        image_urls: 图片 URL 列表(仅图片 LLM 使用)
        image_base64_list: 图片 base64 列表(仅图片 LLM 使用)
        timeout: 调用超时时间(秒),None 表示不设超时
        max_retries: 最大重试次数
        retry_delays: 重试间隔列表(秒),如 [2, 4]

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
                    client.analyze_signal(
                        text,
                        image_urls=image_urls,
                        image_base64_list=image_base64_list,
                        kol_name=kol_name,
                    ),
                    timeout=timeout,
                )
            else:
                result = await client.analyze_signal(
                    text,
                    image_urls=image_urls,
                    image_base64_list=image_base64_list,
                    kol_name=kol_name,
                )
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
            if _is_llm_payment_error(e):
                # 欠费属账户问题,重试无意义: 告警后立即抛出
                await _alert_llm_payment_error(e)
                raise
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
    kol_name: str = "",
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
                kol_name=kol_name,
            )
        except (asyncio.TimeoutError, httpx.HTTPError, ConnectionError, OSError,
                json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.warning(
                f"primary model failed (retried {_MAX_RETRIES}x): {e}, "
                f"switching to fallback and marking degraded"
            )
            _mark_degraded()

    # Tier 2: same-provider fallback (deepseek-v4-pro, non-thinking)
    try:
        fallback = _get_fallback_client(primary_client)
        logger.info(f"using fallback model: {_FALLBACK_MODEL}")
        return await _call_llm_with_retry(
            fallback, text,
            timeout=_FALLBACK_TIMEOUT,
            max_retries=_MAX_RETRIES,
            retry_delays=_RETRY_DELAYS,
            kol_name=kol_name,
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
            kol_name=kol_name,
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


def _detect_468_update_fallback(text: str) -> tuple[bool, str]:
    """468 复查兜底：识别 LLM 容易漏掉的保本/止损更新表达。"""
    raw = text or ""
    patterns = [
        (r"止损\s*(?:放|移|改|设|设置|推|拉).{0,10}(?:开仓价|开仓|成本价|成本|保本)", "止损放开仓价/成本价"),
        (r"(?:放开仓|放成本|放保本|推保护|打保护|做保护)", "保本/成本保护更新"),
        (r"成本保护.{0,12}(?:入场价|开仓价|修改|改到|统一修改)", "成本保护修改"),
        (r"#[A-Za-z0-9_\-]+.{0,12}止损\s*放\s*\d+(?:\.\d+)?", "#标签止损放价"),
        (r"止损\s*放\s*\d+(?:\.\d+)?", "止损放价"),
        (r"(?:浮盈|盈利).{0,16}(?:移动止损|改为开仓价|止损改到开仓价)", "浮盈后移动止损"),
    ]
    for pattern, reason in patterns:
        if re.search(pattern, raw, re.IGNORECASE):
            return True, reason
    return False, ""

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
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    kol_name: str = "",
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
      - 重试机制:LLM 调用失败时最多尝试 3 次,指数退避(2s, 4s)
      - 超时保护:主模型 15s 超时,备用模型 15s 超时
      - 低置信度重试:有效信号 confidence 低于 KOL 阈值时额外重试

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
        min_confidence: 当前 KOL 的最低执行置信度阈值

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
        threshold = max(0.0, min(_as_float(min_confidence, _DEFAULT_MIN_CONFIDENCE), 1.0))
        result: dict[str, Any] = {}
        usage: dict[str, Any] = {}

        # 6. 带降级机制的 LLM 调用(含重试和超时保护)
        #    - 未降级:主模型(15s 超时 + 2次失败重试) -> 失败则备用模型(15s 超时 + 2次失败重试)
        #    - 已降级:直接备用模型 deepseek-v4-pro
        #    - 有效信号但置信度低于阈值:额外重试 2 次,取最后一次返回
        # ★ 优化: 检查 LLM 解析缓存
        _cached_llm = await _get_cached_llm_result(text or "")
        if _cached_llm is not None:
            result = _cached_llm.get("result", {})
            usage = _cached_llm.get("usage", {})
            logger.info(f"[{kol_name}] LLM 缓存命中,跳过 LLM 调用")
        else:
            for low_conf_attempt in range(_LOW_CONFIDENCE_RETRY_COUNT + 1):
                response = await _call_with_degradation(primary_client, final_text, kol_name=kol_name)
                result = response.get("result", {})
                usage = response.get("usage", {})

                # 无效信号不做低置信度重试，避免闲聊/链接/复盘文本浪费 token。
                if not _as_bool(result.get("is_valid_signal", False)):
                    break

                confidence = max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0))
                if confidence >= threshold:
                    break

                if low_conf_attempt < _LOW_CONFIDENCE_RETRY_COUNT:
                    logger.warning(
                        f"文本 LLM 置信度偏低 {confidence:.2f} < 阈值 {threshold:.2f}, "
                        f"重新解析 {low_conf_attempt + 1}/{_LOW_CONFIDENCE_RETRY_COUNT}"
                    )


            # ★ 优化: 缓存 LLM 解析结果
            if _as_bool(result.get("is_valid_signal", False)) or result.get("confidence", 0) > 0:
                await _set_cached_llm_result(text or "", result, usage)

        # 检查是否为有效信号
        if not _as_bool(result.get("is_valid_signal", False)):
            logger.info(f"文本 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text=text,
                confidence=0.0,
            ), usage

        # 构建 ParsedSignal
        is_exit = _as_bool(result.get("is_exit_signal", False))
        is_update = _as_bool(result.get("is_update_signal", False))
        update_reason = str(result.get("reasoning", "") or "")
        fallback_update, fallback_update_reason = _detect_468_update_fallback(stripped_text)
        if fallback_update and not is_exit:
            is_update = True
            update_reason = fallback_update_reason
        position_pct = _as_float(result.get("position_pct"), 0.0)
        if position_pct <= 0:
            # LLM 有时不会返回仓位/平仓比例，使用规则解析兜底识别。
            from app.services.signal_parser import extract_position_pct
            position_pct = extract_position_pct(stripped_text)
        if is_exit and position_pct <= 0:
            # 明确平仓但未说明比例时,默认全部平仓。
            position_pct = 100.0
        parsed = ParsedSignal(
            symbol=_lazy_normalize_symbol(result.get("symbol", "")),  # 统一为 BTC/USDT 格式
            side=_normalize_side(result.get("side", "")),  # 平仓信号若明确多/空,保留方向以避免误平反向仓
            entry_price=None if is_exit else _as_optional_float(result.get("entry_price")),
            entry_prices=[] if is_exit else _as_float_list(result.get("entry_prices", [])),
            take_profits=[] if is_exit else _as_float_list(result.get("take_profits", [])),
            condition_price=None if is_exit else _as_optional_float(result.get("condition_price")),
            breakeven_after_tp=None if is_exit else _as_optional_float(result.get("breakeven_after_tp")),
            stop_loss=None if is_exit else _as_optional_float(result.get("stop_loss")),
            position_pct=max(0.0, min(position_pct, 100.0)),
            raw_text="",
            confidence=max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0)),
            is_exit_signal=is_exit,
            exit_reason=result.get("reasoning", ""),
            is_update_signal=is_update,
            update_reason=update_reason,
            has_image=False,
        )

        if is_exit:
            parsed.actions = ["close_position"]
            parsed.action = "close_position"
        elif is_update:
            parsed.actions = ["update_tp_sl"]
            parsed.action = "update_tp_sl"
            # 更新信号不需要入场价，避免下游误当开仓。
            parsed.entry_price = None
            parsed.entry_prices = []
            parsed.take_profits = _as_float_list(result.get("take_profits", []))
            parsed.stop_loss = _as_optional_float(result.get("stop_loss"))
        elif parsed.side == "long":
            parsed.actions = ["open_long"]
            parsed.action = "open_long"
        elif parsed.side == "short":
            parsed.actions = ["open_short"]
            parsed.action = "open_short"

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
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
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
        min_confidence: 当前 KOL 的最低执行置信度阈值

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
        threshold = max(0.0, min(_as_float(min_confidence, _DEFAULT_MIN_CONFIDENCE), 1.0))
        result: dict[str, Any] = {}
        usage: dict[str, Any] = {}

        # 直接调 vision client 的 analyze_signal(带图片)
        # 图片 LLM 也使用失败重试；有效信号但低置信度时，额外重试 2 次。
        for low_conf_attempt in range(_LOW_CONFIDENCE_RETRY_COUNT + 1):
            response = await _call_llm_with_retry(
                client, text,
                image_urls=image_urls,
                image_base64_list=image_base64_list,
                timeout=_FALLBACK_TIMEOUT,
                max_retries=_MAX_RETRIES,
                retry_delays=_RETRY_DELAYS,
            )
            result = response.get("result", {})
            usage = response.get("usage", {})

            if not _as_bool(result.get("is_valid_signal", False)):
                break

            confidence = max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0))
            if confidence >= threshold:
                break

            if low_conf_attempt < _LOW_CONFIDENCE_RETRY_COUNT:
                logger.warning(
                    f"图片 LLM 置信度偏低 {confidence:.2f} < 阈值 {threshold:.2f}, "
                    f"重新解析 {low_conf_attempt + 1}/{_LOW_CONFIDENCE_RETRY_COUNT}"
                )

        if not _as_bool(result.get("is_valid_signal", False)):
            logger.info(f"图片 LLM 判定为无效信号: {result.get('reasoning', 'N/A')}")
            return ParsedSignal(
                raw_text="",
                confidence=0.0,
                has_image=True,
            ), usage

        position_pct = _as_float(result.get("position_pct"), 0.0)
        is_exit = _as_bool(result.get("is_exit_signal", False))
        is_update = _as_bool(result.get("is_update_signal", False))
        update_reason = str(result.get("reasoning", "") or "")
        # 图片信号没有原始文本，使用 reasoning 作为兜底文本
        fallback_text = str(result.get("reasoning", "") or "")
        fallback_update, fallback_update_reason = _detect_468_update_fallback(fallback_text)
        if fallback_update and not is_exit:
            is_update = True
            update_reason = fallback_update_reason
        if position_pct <= 0:
            # LLM 有时不会返回仓位/平仓比例，使用规则解析兜底。
            from app.services.signal_parser import extract_position_pct
            position_pct = extract_position_pct(fallback_text)
        if is_exit and position_pct <= 0:
            # 明确平仓但未说明比例时,默认全部平仓。
            position_pct = 100.0
        parsed = ParsedSignal(
            symbol=_lazy_normalize_symbol(result.get("symbol", "")),  # 统一为 BTC/USDT 格式
            side=_normalize_side(result.get("side", "")),  # 平仓信号若明确多/空,保留方向以避免误平反向仓
            entry_price=None if is_exit else _as_optional_float(result.get("entry_price")),
            entry_prices=[] if is_exit else _as_float_list(result.get("entry_prices", [])),
            take_profits=[] if is_exit else _as_float_list(result.get("take_profits", [])),
            condition_price=None if is_exit else _as_optional_float(result.get("condition_price")),
            breakeven_after_tp=None if is_exit else _as_optional_float(result.get("breakeven_after_tp")),
            stop_loss=None if is_exit else _as_optional_float(result.get("stop_loss")),
            position_pct=max(0.0, min(position_pct, 100.0)),
            raw_text="",
            confidence=max(0.0, min(_as_float(result.get("confidence"), 0.5), 1.0)),
            is_exit_signal=is_exit,
            exit_reason=result.get("reasoning", ""),
            is_update_signal=is_update,
            update_reason=update_reason,
            has_image=True,
        )

        if is_exit:
            parsed.actions = ["close_position"]
            parsed.action = "close_position"
        elif is_update:
            parsed.actions = ["update_tp_sl"]
            parsed.action = "update_tp_sl"
            # 更新信号不需要入场价，避免下游误当开仓。
            parsed.entry_price = None
            parsed.entry_prices = []
            parsed.take_profits = _as_float_list(result.get("take_profits", []))
            parsed.stop_loss = _as_optional_float(result.get("stop_loss"))
        elif parsed.side == "long":
            parsed.actions = ["open_long"]
            parsed.action = "open_long"
        elif parsed.side == "short":
            parsed.actions = ["open_short"]
            parsed.action = "open_short"

        if not parsed.entry_price and parsed.entry_prices:
            parsed.entry_price = parsed.entry_prices[0]

        # 如果没有品种信息，尝试从文本提取
        if not parsed.symbol:
            parsed.symbol = _extract_symbol_fallback(fallback_text)

        # 借鉴 parse_with_llm 的解析保护: 用止盈/入场/止损的价格关系自动纠正多空方向。
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
                    f"图片 LLM 方向纠正: short→long (TP={tp_price}>EP={ep}>SL={sl})"
                )
                parsed.side = "long"
            elif tp_price < ep < sl and parsed.side == "long":
                logger.warning(
                    f"图片 LLM 方向纠正: long→short (TP={tp_price}<EP={ep}<SL={sl})"
                )
                parsed.side = "short"

        logger.info(
            f"图片 LLM 解析成功: symbol={parsed.symbol}, side={parsed.side}, "
            f"is_exit={parsed.is_exit_signal}, confidence={parsed.confidence}, "
            f"tokens={usage.get('total_tokens', 0)}"
        )
        return parsed, usage

    except Exception as e:
        logger.error(f"图片 LLM 解析异常: {e}")
        return None, {}
