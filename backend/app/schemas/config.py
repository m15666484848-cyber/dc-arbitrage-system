"""配置(交易所账号/风控/告警)schemas。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field, ConfigDict


class ExchangeAccountOut(BaseModel):
    id: int
    customer_id: int
    exchange: str
    label: str = ""
    testnet: bool = False
    account_mode: str = "live"  # live|testnet|demo
    is_active: bool = True
    is_default: bool = False
    follow_enabled: bool = False
    follow_weight: float = Field(1.0, ge=0)
    max_order_usdt: float = Field(0.0, ge=0)
    position_mode: str = "fixed"  # fixed|equity_pct|fixed_amount
    position_pct: float = Field(0.0, ge=0)
    fixed_amount_usdt: float = Field(0.0, ge=0)
    strategy_id: int | None = None
    last_error: str = ""
    last_verified_at: datetime | None = None
    # 脱敏:仅显示 key 前后几位
    api_key_mask: str = ""
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExchangeAccountCreate(BaseModel):
    exchange: str  # okx|binance|bybit
    label: str = ""
    api_key: str
    api_secret: str
    passphrase: str = ""  # OKX
    testnet: bool = False
    account_mode: str | None = None  # live|testnet|demo；为空时兼容旧 testnet 字段
    follow_enabled: bool = False
    follow_weight: float = Field(default=1.0, ge=0, le=100)
    max_order_usdt: float = Field(default=0.0, ge=0)
    position_mode: str = "fixed"  # fixed|equity_pct|fixed_amount
    position_pct: float = Field(default=0.0, ge=0, le=5)
    fixed_amount_usdt: float = Field(default=0.0, ge=0)
    strategy_id: int | None = None


class ExchangeAccountFollowUpdate(BaseModel):
    """更新单个 API 的多 API 跟单配置。"""

    follow_enabled: bool | None = None
    follow_weight: float | None = Field(default=None, ge=0, le=100)
    max_order_usdt: float | None = Field(default=None, ge=0)
    position_mode: str | None = Field(default=None, pattern="^(fixed|equity_pct|fixed_amount)$")
    position_pct: float | None = Field(default=None, ge=0, le=5)
    fixed_amount_usdt: float | None = Field(default=None, ge=0)
    strategy_id: int | None = None


class DiscordAccountOut(BaseModel):
    id: int
    label: str = "默认 Discord 账号"
    token_mask: str = ""
    token_set: bool = False
    enabled: bool = True
    is_default: bool = False
    last_error: str = ""
    last_connected_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscordAccountCreate(BaseModel):
    label: str = "Discord 账号"
    token: str
    enabled: bool = True
    is_default: bool = False


class DiscordAccountUpdate(BaseModel):
    label: str | None = None
    token: str | None = None  # None=不改,""=清空(会被拒绝),其他=新值
    enabled: bool | None = None
    is_default: bool | None = None


class RiskConfigOut(BaseModel):
    id: int
    customer_id: int
    exchange: str = "all"
    silent_ranges: list[dict[str, Any]] = []
    silent_action: str = "ignore"
    max_position_usdt: float = Field(0.0, ge=0)
    max_concurrent_positions: int = 0
    max_daily_loss_pct: float = Field(0.0, ge=0, le=100)
    per_kol_max_usdt: float = Field(0.0, ge=0)
    enabled: bool = True
    # 客户级风控开关
    position_timeout_hours: int = Field(72, ge=0)
    consecutive_loss_threshold: int = Field(3, ge=0)
    consecutive_loss_pause_hours: int = Field(24, ge=0)
    kol_frequency_per_hour: int = 20
    auto_stop_loss_pct: float = Field(5.0, ge=0, le=100)
    enable_trailing_stop: bool = False
    trailing_callback_pct: float = Field(1.0, ge=0, le=100)
    cooldown_minutes: int = Field(60, ge=0)

    model_config = ConfigDict(from_attributes=True)


class RiskConfigUpdate(BaseModel):
    exchange: str = "all"
    silent_ranges: list[dict[str, Any]] = []
    silent_action: str = "ignore"
    max_position_usdt: float = 0.0
    max_concurrent_positions: int = 0
    max_daily_loss_pct: float = 0.0
    per_kol_max_usdt: float = 0.0
    enabled: bool = True
    # 客户级风控开关
    position_timeout_hours: int = 72
    consecutive_loss_threshold: int = 3
    consecutive_loss_pause_hours: int = 24
    kol_frequency_per_hour: int = 20
    auto_stop_loss_pct: float = 5.0
    enable_trailing_stop: bool = False
    trailing_callback_pct: float = 1.0
    cooldown_minutes: int = 60


class AlertConfigOut(BaseModel):
    id: int
    customer_id: int | None = None
    name: str = "飞书告警"
    webhook_url: str = ""
    # 从 ORM 读取但不输出密钥本身(脱敏)
    webhook_secret: str = Field(default="", exclude=True)
    enabled: bool = True
    on_signal: bool = False
    on_order: bool = True
    on_tp_sl: bool = True
    on_correct: bool = True
    on_risk: bool = True
    on_auth_expire: bool = True
    on_error: bool = True

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def webhook_secret_set(self) -> bool:
        """是否已配置签名密钥(不回显密钥本身)。"""
        return bool(self.webhook_secret)


class AlertConfigCreate(BaseModel):
    name: str = "飞书告警"
    webhook_url: str
    # 飞书机器人签名校验密钥(留空表示不校验/不改;显式传 "" 表示清空)
    webhook_secret: str | None = None
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
