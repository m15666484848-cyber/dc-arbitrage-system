"""配置(交易所账号/风控/告警)schemas。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ExchangeAccountOut(BaseModel):
    id: int
    customer_id: int
    exchange: str
    label: str = ""
    testnet: bool = False
    is_active: bool = True
    last_error: str = ""
    # 脱敏:仅显示 key 前后几位
    api_key_mask: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class ExchangeAccountCreate(BaseModel):
    exchange: str  # okx|binance|bybit
    label: str = ""
    api_key: str
    api_secret: str
    passphrase: str = ""  # OKX
    testnet: bool = False


class RiskConfigOut(BaseModel):
    id: int
    customer_id: int
    exchange: str = "all"
    silent_ranges: list[dict[str, Any]] = []
    silent_action: str = "ignore"
    max_position_usdt: float = 0.0
    max_concurrent_positions: int = 0
    max_daily_loss_pct: float = 0.0
    per_kol_max_usdt: float = 0.0
    enabled: bool = True

    class Config:
        from_attributes = True


class RiskConfigUpdate(BaseModel):
    exchange: str = "all"
    silent_ranges: list[dict[str, Any]] = []
    silent_action: str = "ignore"
    max_position_usdt: float = 0.0
    max_concurrent_positions: int = 0
    max_daily_loss_pct: float = 0.0
    per_kol_max_usdt: float = 0.0
    enabled: bool = True


class AlertConfigOut(BaseModel):
    id: int
    customer_id: int | None = None
    name: str = "飞书告警"
    webhook_url: str = ""
    enabled: bool = True
    on_signal: bool = False
    on_order: bool = True
    on_tp_sl: bool = True
    on_correct: bool = True
    on_risk: bool = True
    on_auth_expire: bool = True
    on_error: bool = True

    class Config:
        from_attributes = True


class AlertConfigCreate(BaseModel):
    name: str = "飞书告警"
    webhook_url: str
    enabled: bool = True
    on_signal: bool = False
    on_order: bool = True
    on_tp_sl: bool = True
    on_correct: bool = True
    on_risk: bool = True
    on_auth_expire: bool = True
    on_error: bool = True


class EquityPoint(BaseModel):
    snapshot_at: datetime
    equity: float
    balance: float
    unrealized_pnl: float = 0.0


# ---------- 系统配置(双 LLM + Discord) ----------
class SystemConfigOut(BaseModel):
    """系统配置(脱敏输出)。

    api_key_mask / token_mask 为空表示未配置;有值则仅显示前后几位。
    """
    # 全局开关
    llm_enabled: bool = False
    # 文本 LLM(DeepSeek V3)
    text_llm_provider: str = "deepseek"
    text_llm_api_key_mask: str = ""
    text_llm_api_key_set: bool = False
    text_llm_model: str = ""
    text_llm_api_base: str = ""
    text_llm_temperature: float = 0.1
    text_llm_max_tokens: int = 2000
    text_llm_timeout: int = 30
    # 图片 LLM(GLM-4V)
    vision_llm_enabled: bool = False
    vision_llm_provider: str = "zhipu"
    vision_llm_api_key_mask: str = ""
    vision_llm_api_key_set: bool = False
    vision_llm_model: str = ""
    vision_llm_api_base: str = ""
    vision_llm_temperature: float = 0.1
    vision_llm_max_tokens: int = 2000
    vision_llm_timeout: int = 60
    # Discord
    discord_token_mask: str = ""
    discord_token_set: bool = False
    discord_heartbeat_interval: int = 41


class SystemConfigUpdate(BaseModel):
    """更新系统配置。

    api_key / token 留空表示不修改(保留原值);
    显式传空字符串 "" 表示清空。
    """
    # 全局开关
    llm_enabled: bool | None = None
    # 文本 LLM
    text_llm_provider: str | None = None
    text_llm_api_key: str | None = None  # None=不改,""=清空,其他=新值
    text_llm_model: str | None = None
    text_llm_api_base: str | None = None
    text_llm_temperature: float | None = None
    text_llm_max_tokens: int | None = None
    text_llm_timeout: int | None = None
    # 图片 LLM
    vision_llm_enabled: bool | None = None
    vision_llm_provider: str | None = None
    vision_llm_api_key: str | None = None
    vision_llm_model: str | None = None
    vision_llm_api_base: str | None = None
    vision_llm_temperature: float | None = None
    vision_llm_max_tokens: int | None = None
    vision_llm_timeout: int | None = None
    # Discord
    discord_token: str | None = None
    discord_heartbeat_interval: int | None = None


class LLMTestResult(BaseModel):
    success: bool
    message: str
    latency_ms: int = 0
    tokens_used: int = 0
