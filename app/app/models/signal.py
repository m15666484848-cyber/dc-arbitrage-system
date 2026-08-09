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
