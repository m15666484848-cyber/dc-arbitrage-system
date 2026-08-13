"""待触发限价单模型(服务端限价单)。"""
from datetime import datetime

from sqlalchemy import Float, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class PendingOrder(Base, TimestampMixin):
    """服务端限价单:入场价远离市价时,存表等待价格触及后触发市价下单。

    status: pending(等待中) / triggered(已触发) / cancelled(已取消) / expired(已过期)
    """

    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kol_id: Mapped[int] = mapped_column(ForeignKey("kols.id", ondelete="SET NULL"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"), nullable=True)

    # 交易参数
    exchange_account_id: Mapped[int | None] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(16))  # long|short
    entry_price: Mapped[float] = mapped_column(Float)  # 目标入场价
    condition_price: Mapped[float | None] = mapped_column(nullable=True)  # 先触及的条件价
    condition_direction: Mapped[str] = mapped_column(String(16), default="")  # up|down
    trigger_mode: Mapped[str] = mapped_column(String(32), default="entry")  # entry|condition_then_entry
    condition_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notional_usdt: Mapped[float] = mapped_column(Float)  # 名义价值
    leverage: Mapped[int] = mapped_column(Integer, default=1)

    # 止盈止损配置(触发下单时使用)
    tp_levels: Mapped[list] = mapped_column(JSONB, default=list)  # [{level, price, pct}]
    sl: Mapped[float | None] = mapped_column(nullable=True)
    strategy_params: Mapped[dict] = mapped_column(JSONB, default=dict)  # 策略默认参数

    # 状态管理
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 过期时间
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    triggered_position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
