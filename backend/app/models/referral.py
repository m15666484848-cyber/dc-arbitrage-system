"""邀请佣金模型:记录邀请人从下级利润中获得的提成。"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class ReferralCommission(Base, TimestampMixin):
    """邀请佣金:下级客户平仓盈利时,邀请人获得固定比例提成。

    佣金仅在邀请下级有正盈利(净盈亏 > 0)时产生,
    亏损不扣减(不做负佣金)。
    """

    __tablename__ = "referral_commissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 邀请人(获得佣金的一方)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    # 被邀请人(产生利润的一方)
    invitee_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    # 关联的平仓 Trade 记录
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id", ondelete="SET NULL"), nullable=True)
    # 下级的净盈亏(USDT)
    invitee_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    # 佣金比例(如 0.1 = 10%)
    commission_rate: Mapped[float] = mapped_column(Float, default=0.1)
    # 佣金金额(USDT) = invitee_pnl * commission_rate
    commission_amount: Mapped[float] = mapped_column(Float, default=0.0)
    # 交易品种(冗余,方便查询)
    symbol: Mapped[str] = mapped_column(String(64), default="")
    # 备注
    note: Mapped[str] = mapped_column(Text, default="")
    # 佣金产生时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
