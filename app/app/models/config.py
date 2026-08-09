"""配置类模型:交易所账号、风控、告警、净值快照。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class ExchangeAccount(Base, TimestampMixin):
    """客户绑定的交易所账号(API Key 加密存储)。

    防共用机制:
      - api_key_hash: API Key 的 SHA256 哈希,唯一索引,防止同一 API Key 被多个客户绑定
      - 业务层校验:同客户同交易所只能绑 1 个 API(除非 multi_exchange_allowed)
    """

    __tablename__ = "exchange_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)  # okx|binance|bybit
    label: Mapped[str] = mapped_column(String(64), default="")
    api_key_enc: Mapped[str] = mapped_column(Text)
    api_secret_enc: Mapped[str] = mapped_column(Text)
    passphrase_enc: Mapped[str] = mapped_column(String(512), default="")  # OKX 专用
    # API Key 的 SHA256 哈希(不可逆),用于跨客户唯一性校验,防止多人共用同一 API Key
    # 不加 DB 唯一约束(避免旧数据空值冲突),业务层校验唯一性
    api_key_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    testnet: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class RiskConfig(Base, TimestampMixin):
    """客户风控配置(含静默时段)。

    silent_ranges: [{"start":"23:00","end":"07:00"}, ...]  # 静默时段,期间只记录不下单
    silent_action: ignore(忽略) / delay(延迟到开盘补单) / log_only(仅记录)
    """

    __tablename__ = "risk_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    exchange: Mapped[str] = mapped_column(String(32), default="all")
    silent_ranges: Mapped[list] = mapped_column(JSONB, default=list)
    silent_action: Mapped[str] = mapped_column(String(16), default="ignore")
    max_position_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 0=不限
    max_concurrent_positions: Mapped[int] = mapped_column(Integer, default=0)  # 0=不限
    max_daily_loss_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 0=不限
    per_kol_max_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertConfig(Base, TimestampMixin):
    """飞书 Webhook 告警配置。"""

    __tablename__ = "alert_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    # customer_id 为空则为管理员全局告警
    name: Mapped[str] = mapped_column(String(64), default="飞书告警")
    webhook_url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 触发事件开关
    on_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    on_order: Mapped[bool] = mapped_column(Boolean, default=True)
    on_tp_sl: Mapped[bool] = mapped_column(Boolean, default=True)
    on_correct: Mapped[bool] = mapped_column(Boolean, default=True)
    on_risk: Mapped[bool] = mapped_column(Boolean, default=True)
    on_auth_expire: Mapped[bool] = mapped_column(Boolean, default=True)
    on_error: Mapped[bool] = mapped_column(Boolean, default=True)


class EquitySnapshot(Base, TimestampMixin):
    """账户净值快照(用于走势图,定时落库)。"""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    equity: Mapped[float] = mapped_column(Float)  # 账户权益(USDT)
    balance: Mapped[float] = mapped_column(Float)  # 余额
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SystemConfig(Base, TimestampMixin):
    """全局系统配置(单行表,运行时可改)。

    存储 LLM / Discord 等敏感配置,API Key/Token 用 Fernet 加密。
    数据库配置优先级高于 .env,允许通过管理界面热更新。

    双 LLM 架构:
      - text_llm_*:文本信号解析(推荐 DeepSeek V3,便宜稳定)
      - vision_llm_*:图片信号解析(推荐 GLM-4V,多模态)
      - 全局 llm_enabled 关闭时,两个模型都不调用
      - 图片 LLM 仅对 KOL.vision_llm_enabled=True 的 KOL 生效
    """

    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)  # 固定为 1,单行表
    # ---- LLM 全局开关 ----
    llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---- 文本 LLM(推荐 DeepSeek V3) ----
    text_llm_provider: Mapped[str] = mapped_column(String(32), default="deepseek")
    text_llm_api_key_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet 加密
    text_llm_model: Mapped[str] = mapped_column(String(64), default="")  # 空则用预设
    text_llm_api_base: Mapped[str] = mapped_column(String(256), default="")  # 空则用预设
    text_llm_temperature: Mapped[float] = mapped_column(Float, default=0.1)
    text_llm_max_tokens: Mapped[int] = mapped_column(Integer, default=2000)
    text_llm_timeout: Mapped[int] = mapped_column(Integer, default=30)
    # ---- 图片 LLM(推荐 GLM-4V,多模态) ----
    vision_llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    vision_llm_provider: Mapped[str] = mapped_column(String(32), default="zhipu")
    vision_llm_api_key_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet 加密
    vision_llm_model: Mapped[str] = mapped_column(String(64), default="")
    vision_llm_api_base: Mapped[str] = mapped_column(String(256), default="")
    vision_llm_temperature: Mapped[float] = mapped_column(Float, default=0.1)
    vision_llm_max_tokens: Mapped[int] = mapped_column(Integer, default=2000)
    vision_llm_timeout: Mapped[int] = mapped_column(Integer, default=60)
    # ---- Discord ----
    discord_token_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet 加密
    discord_heartbeat_interval: Mapped[int] = mapped_column(Integer, default=41)
