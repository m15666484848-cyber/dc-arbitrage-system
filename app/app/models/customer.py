"""客户模型与时间授权。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin


class Customer(Base, TimestampMixin):
    """客户(跟单交易者,仅可见自身数据)。

    防共用机制:
      - 默认 multi_exchange_allowed=False,只能绑 1 个交易所 1 个 API
      - 多开(绑多个交易所)需管理员授权
      - max_order_usdt 由管理员下发,强制限制单笔下单上限(默认 5000 USDT)
    """

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # ---- 防共用控制 ----
    # 是否允许绑多个交易所(多开),默认 False,需管理员授权
    multi_exchange_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 单笔下单上限(USDT),管理员强制下发,优先级高于客户自配的 RiskConfig
    max_order_usdt: Mapped[float] = mapped_column(Float, default=5000.0)

    authorizations: Mapped[list["Authorization"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    @property
    def role(self) -> str:
        return "customer"


class Authorization(Base, TimestampMixin):
    """时间授权:未授权或过期则禁止下单(可分交易所)。

    exchange 为 'all' 表示对所有交易所生效。
    """

    __tablename__ = "authorizations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    exchange: Mapped[str] = mapped_column(String(32), default="all", index=True)  # all|okx|binance|bybit
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")

    customer: Mapped["Customer"] = relationship(back_populates="authorizations")

    def is_valid_now(self, now: datetime) -> bool:
        return self.active and self.starts_at <= now <= self.expires_at
