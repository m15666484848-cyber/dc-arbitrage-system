"""运行时配置读取模块。

优先级:数据库 SystemConfig > .env settings > 默认值
带 5 秒缓存,避免高频读库。

双 LLM 架构:
  - text_llm:文本信号解析(默认 DeepSeek V3)
  - vision_llm:图片信号解析(默认 GLM-4V)
  - 图片 LLM 仅对 KOL.vision_llm_enabled=True 生效
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import decrypt_secret, encrypt_secret
from app.models.config import SystemConfig


_CACHE_TTL = 5  # 秒
_cache_ts: float = 0
_cache: Optional[SystemConfig] = None


@dataclass
class TextLLMSettings:
    """文本 LLM 配置快照(用于信号文本解析)。"""
    enabled: bool  # 跟随全局 llm_enabled
    provider: str
    api_key: str  # 解密后
    model: str
    api_base: str
    temperature: float
    max_tokens: int
    timeout: int


@dataclass
class VisionLLMSettings:
    """图片 LLM 配置快照(用于图片信号解析)。"""
    enabled: bool  # vision_llm_enabled 且 全局 llm_enabled
    provider: str
    api_key: str
    model: str
    api_base: str
    temperature: float
    max_tokens: int
    timeout: int


@dataclass
class DiscordSettings:
    """Discord 运行时配置快照。"""
    token: str  # 解密后
    heartbeat_interval: int


async def _load_db_config() -> Optional[SystemConfig]:
    """从数据库加载 SystemConfig(单行表,id=1)。"""
    global _cache, _cache_ts
    now = time.time()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache
    try:
        async with AsyncSessionLocal() as db:
            cfg = (await db.execute(select(SystemConfig).where(SystemConfig.id == 1))).scalar_one_or_none()
            _cache = cfg
            _cache_ts = now
            return cfg
    except Exception as e:
        logger.debug(f"读取 SystemConfig 失败,回退到 .env: {e}")
        return None


def invalidate_cache() -> None:
    """使缓存失效(配置更新后调用)。"""
    global _cache_ts
    _cache_ts = 0


# 预设默认值
_PROVIDER_DEFAULTS = {
    "deepseek": {
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
    },
    "zhipu": {
        "model": "glm-4v",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
    },
    "glm": {
        "model": "glm-4v",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
    },
    "siliconflow": {
        "model": "zai-org/GLM-4.5V",
        "api_base": "https://api.siliconflow.cn/v1",
    },
}


async def get_text_llm_settings() -> TextLLMSettings:
    """获取文本 LLM 配置。

    优先级:数据库 text_llm_* > .env llm_* > 默认。
    """
    cfg = await _load_db_config()

    enabled = settings.llm_enabled
    provider = settings.llm_provider
    api_key = settings.llm_api_key
    model = settings.llm_model or ""
    api_base = settings.llm_api_base or ""
    temperature = settings.llm_temperature
    max_tokens = settings.llm_max_tokens
    timeout = settings.llm_timeout

    if cfg:
        enabled = cfg.llm_enabled
        if cfg.text_llm_provider:
            provider = cfg.text_llm_provider
        if cfg.text_llm_api_key_enc:
            try:
                api_key = decrypt_secret(cfg.text_llm_api_key_enc)
            except Exception:
                logger.warning("text_llm API Key 解密失败,使用 .env 值")
        if cfg.text_llm_model:
            model = cfg.text_llm_model
        if cfg.text_llm_api_base:
            api_base = cfg.text_llm_api_base
        temperature = cfg.text_llm_temperature
        max_tokens = cfg.text_llm_max_tokens
        timeout = cfg.text_llm_timeout

    # 预设默认值
    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["deepseek"])
    if not model:
        model = defaults["model"]
    if not api_base:
        api_base = defaults["api_base"]

    return TextLLMSettings(
        enabled=enabled,
        provider=provider,
        api_key=api_key,
        model=model,
        api_base=api_base,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def get_vision_llm_settings() -> VisionLLMSettings:
    """获取图片 LLM 配置。

    优先级:数据库 vision_llm_* > .env vision_llm_* > zhipu 默认。
    enabled 需要同时满足:全局 llm_enabled 且 vision_llm_enabled。
    """
    cfg = await _load_db_config()

    # .env fallback (settings.vision_llm_*)
    global_enabled = settings.llm_enabled
    enabled = settings.llm_enabled and settings.vision_llm_enabled
    provider = settings.vision_llm_provider
    api_key = settings.vision_llm_api_key
    model = settings.vision_llm_model
    api_base = settings.vision_llm_api_base
    temperature = settings.vision_llm_temperature
    max_tokens = settings.vision_llm_max_tokens
    timeout = settings.vision_llm_timeout

    if cfg:
        global_enabled = cfg.llm_enabled
        enabled = global_enabled and cfg.vision_llm_enabled
        if cfg.vision_llm_provider:
            provider = cfg.vision_llm_provider
        if cfg.vision_llm_api_key_enc:
            try:
                api_key = decrypt_secret(cfg.vision_llm_api_key_enc)
            except Exception:
                logger.warning("vision_llm API Key 解密失败")
        if cfg.vision_llm_model:
            model = cfg.vision_llm_model
        if cfg.vision_llm_api_base:
            api_base = cfg.vision_llm_api_base
        temperature = cfg.vision_llm_temperature
        max_tokens = cfg.vision_llm_max_tokens
        timeout = cfg.vision_llm_timeout

    # 如果 vision 没配 api_key,回退到 text_llm 的 key(zhipu 和 deepseek 通用 OpenAI 兼容接口)
    if not api_key:
        text_cfg = await get_text_llm_settings()
        if text_cfg.provider == provider:
            api_key = text_cfg.api_key

    defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["zhipu"])
    if not model:
        model = defaults["model"]
    if not api_base:
        api_base = defaults["api_base"]

    return VisionLLMSettings(
        enabled=enabled,
        provider=provider,
        api_key=api_key,
        model=model,
        api_base=api_base,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


async def get_discord_settings() -> DiscordSettings:
    """获取 Discord 运行时配置。

    优先级:数据库 > .env。
    """
    cfg = await _load_db_config()

    token = settings.discord_token
    heartbeat = 41

    if cfg:
        if cfg.discord_token_enc:
            try:
                token = decrypt_secret(cfg.discord_token_enc)
            except Exception:
                logger.warning("Discord Token 解密失败,使用 .env 值")
        heartbeat = cfg.discord_heartbeat_interval or heartbeat

    return DiscordSettings(token=token, heartbeat_interval=heartbeat)


async def ensure_system_config_row() -> SystemConfig:
    """确保数据库有一行 SystemConfig(id=1),不存在则创建。"""
    from sqlalchemy import select as _select

    async with AsyncSessionLocal() as db:
        cfg = (await db.execute(_select(SystemConfig).where(SystemConfig.id == 1))).scalar_one_or_none()
        if cfg:
            return cfg
        cfg = SystemConfig(id=1)
        db.add(cfg)
        await db.commit()
        logger.info("已初始化 SystemConfig 单行记录")
        return cfg
