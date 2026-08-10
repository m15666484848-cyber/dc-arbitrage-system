"""配置类模型:交易所账号、风控、告警、净值快照。"""
from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    account_mode: Mapped[str] = mapped_column(String(16), default="live")  # live|testnet|demo
    account_type: Mapped[str] = mapped_column(String(16), default="")  # Bybit: unified|classic(空=自动)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 多 API 跟单配置
    follow_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    follow_weight: Mapped[float] = mapped_column(Float, default=1.0)
    max_order_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # 0=不限
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True)


class DiscordAccount(Base, TimestampMixin):
    """Discord 监听账号(Token 加密存储)。

    - token_enc: Fernet 加密后的 Discord Token
    - token_hash: Token SHA256 哈希,用于检测重复和热重载
    - is_default: 默认账号,兼容未绑定账号的旧 KOL
    """

    __tablename__ = "discord_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), default="默认 Discord 账号")
    token_enc: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    kols: Mapped[list["Kol"]] = relationship("Kol", back_populates="discord_account")


class RiskConfig(Base, TimestampMixin):
    """客户风控配置(含静默时段、超时平仓、连亏暂停、自动止损)。

    silent_ranges: [{"start":"23:00","end":"07:00"}, ...]  # 静默时段,期间只记录不下单
    silent_action: ignore(忽略) / delay(延迟到开盘补单) / log_only(仅记录)

    客户级风控开关(0/False=禁用):
      - position_timeout_hours: 持仓超时自动平仓小时数(0=禁用,默认48)
      - consecutive_loss_threshold: 连亏N次后暂停该KOL(0=禁用,默认5)
      - consecutive_loss_pause_hours: 暂停时长(默认24)
      - kol_frequency_per_hour: KOL每小时信号上限(0=禁用,默认30)
      - auto_stop_loss_pct: 自动止损补充百分比(0=禁用,默认5%)
      - enable_trailing_stop: 启用追踪止损
      - trailing_callback_pct: 追踪止损回撤比例(默认1%)
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
    # ---- 持仓超时自动平仓 ----
    position_timeout_hours: Mapped[int] = mapped_column(Integer, default=72)  # 0=禁用
    # ---- 连亏暂停 ----
    consecutive_loss_threshold: Mapped[int] = mapped_column(Integer, default=3)  # 0=禁用
    consecutive_loss_pause_hours: Mapped[int] = mapped_column(Integer, default=24)
    # ---- KOL 频率限制 ----
    kol_frequency_per_hour: Mapped[int] = mapped_column(Integer, default=20)  # 0=禁用
    # ---- 自动止损补充 ----
    auto_stop_loss_pct: Mapped[float] = mapped_column(Float, default=5.0)  # 0=禁用,百分比
    # ---- 追踪止损 ----
    enable_trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_callback_pct: Mapped[float] = mapped_column(Float, default=1.0)  # 百分比


class AlertConfig(Base, TimestampMixin):
    """飞书 Webhook 告警配置。"""

    __tablename__ = "alert_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    # customer_id 为空则为管理员全局告警
    name: Mapped[str] = mapped_column(String(64), default="飞书告警")
    webhook_url: Mapped[str] = mapped_column(String(512))
    # 飞书自定义机器人签名校验密钥(在机器人安全设置中开启"签名校验"后填入)。
    # 设置后每条消息携带 timestamp+sign,防止 webhook 被伪造调用。
    webhook_secret: Mapped[str] = mapped_column(String(256), default="")
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




class DailyRiskSnapshot(Base, TimestampMixin):
    """日风控快照:按北京时间自然日沉淀每日亏损、浮亏和风控触发状态。"""

    __tablename__ = "daily_risk_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    exchange: Mapped[str] = mapped_column(String(32), default="all", index=True)
    day: Mapped[datetime.date] = mapped_column(Date, index=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=0.0)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    base_equity: Mapped[float] = mapped_column(Float, default=0.0)
    max_daily_loss_pct: Mapped[float] = mapped_column(Float, default=0.0)
    loss_pct: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(16), default="normal")
    risk_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    close_count: Mapped[int] = mapped_column(Integer, default=0)
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
