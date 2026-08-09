"""KOL 档案与客户关注关系。"""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin


class Kol(Base, TimestampMixin):
    """KOL 档案:绑定 Discord 频道号与用户 ID。"""

    __tablename__ = "kols"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    discord_channel_id: Mapped[str] = mapped_column(String(64), index=True)
    discord_user_id: Mapped[str] = mapped_column(String(64), default="")  # 空则监听频道所有人
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    avatar: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    # ============ LLM 解析配置 ============
    # 是否启用 LLM 解析（覆盖全局设置）
    llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否对该 KOL 启用图片 LLM 分析（仅图片信号走 vision LLM）
    vision_llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 规则解析失败时是否降级到 LLM
    llm_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    # 最低置信度阈值（低于此值才触发 LLM 兜底）
    llm_min_confidence: Mapped[float] = mapped_column(Float, default=0.4)

    # ============ 历史统计 ============
    # 历史胜率/统计缓存(由 analytics 定期刷新)
    cached_win_rate: Mapped[float] = mapped_column(default=0.0)
    cached_pnl: Mapped[float] = mapped_column(default=0.0)
    cached_signal_count: Mapped[int] = mapped_column(default=0)

    # LLM 调用统计
    llm_calls_total: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls_success: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_used: Mapped[int] = mapped_column(Integer, default=0)

    follows: Mapped[list["KolFollow"]] = relationship(
        back_populates="kol", cascade="all, delete-orphan"
    )


class KolFollow(Base, TimestampMixin):
    """客户关注的 KOL(多选/全选),可绑定独立策略和跟单金额。

    followed_notional_usdt: 客户自定义跟单金额(USDT),为 NULL 或 0 时使用策略中的 base_qty。
    """

    __tablename__ = "kol_follows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kol_id: Mapped[int] = mapped_column(ForeignKey("kols.id", ondelete="CASCADE"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    followed_notional_usdt: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    kol: Mapped["Kol"] = relationship(back_populates="follows")
