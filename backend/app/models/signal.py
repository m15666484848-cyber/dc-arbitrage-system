"""原始信号模型。"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class Signal(Base, TimestampMixin):
    """来自 Discord 的原始策略信号及其解析结果。

    status: received(已收) / parsed(已解析) / filtered(被过滤) / corrected(已纠错)
            / ordered(已下单) / rejected(拒绝) / ignored(忽略)
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kol_id: Mapped[int] = mapped_column(ForeignKey("kols.id", ondelete="CASCADE"), index=True)
    discord_message_id: Mapped[str] = mapped_column(String(64), index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(512), default="")
    # 解析后的结构化数据(符号/方向/入场/止盈多级/止损/杠杆等)
    parsed: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    corrected: Mapped[bool] = mapped_column(default=False)
    correct_log: Mapped[str] = mapped_column(Text, default="")  # 纠错轨迹说明
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 置信度评分 0-1
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # 解析字段冗余(便于查询/排序)
    symbol: Mapped[str] = mapped_column(String(64), default="", index=True)
    side: Mapped[str] = mapped_column(String(16), default="")  # long|short
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class ParserShadowResult(Base, TimestampMixin):
    """影子解析结果。

    只记录新旧解析对比和人工审核状态，不参与真实下单链路。
    """

    __tablename__ = "parser_shadow_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True)
    kol_id: Mapped[int | None] = mapped_column(ForeignKey("kols.id", ondelete="SET NULL"), nullable=True, index=True)
    discord_message_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(32), default="live", index=True)
    parse_version: Mapped[str] = mapped_column(String(64), default="", index=True)

    old_parsed: Mapped[dict] = mapped_column(JSONB, default=dict)
    new_parsed: Mapped[dict] = mapped_column(JSONB, default=dict)
    diff: Mapped[dict] = mapped_column(JSONB, default=dict)
    mismatch_fields: Mapped[list] = mapped_column(JSONB, default=list)

    old_status: Mapped[str] = mapped_column(String(32), default="")
    new_status: Mapped[str] = mapped_column(String(32), default="")
    old_symbol: Mapped[str] = mapped_column(String(64), default="", index=True)
    new_symbol: Mapped[str] = mapped_column(String(64), default="", index=True)
    old_side: Mapped[str] = mapped_column(String(16), default="")
    new_side: Mapped[str] = mapped_column(String(16), default="")
    old_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    old_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signal_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
