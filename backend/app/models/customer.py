"""客户模型与时间授权。"""
import secrets
from datetime import datetime

from sqlalchemy import Numeric, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import backref, Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin


def _generate_invite_code() -> str:
    """生成 8 位邀请码(大写字母+数字)。"""
    return secrets.token_hex(4).upper()


class Customer(Base, TimestampMixin):
    """客户(跟单交易者,仅可见自身数据)。

    账号状态流转:
      pending → (管理员审批) → active (可正常使用)
      pending → (管理员拒绝) → rejected (不可使用,需重新注册)

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
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # 账号状态: pending(待审批) | active(已激活) | rejected(已拒绝)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 注册来源: self(自助注册) | admin(管理员创建) | invite(邀请码)
    register_source: Mapped[str] = mapped_column(String(16), default="self")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 管理员备注 / 拒绝原因
    note: Mapped[str] = mapped_column(Text, default="")
    reject_reason: Mapped[str] = mapped_column(String(500), default="")
    # ---- 防共用控制 ----
    # 是否允许绑多个交易所(多开),默认 False,需管理员授权
    # Whether same exchange/mode can bind multiple API keys; admin controlled.
    single_exchange_multi_api_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    single_exchange_multi_api_limit: Mapped[int] = mapped_column(Integer, default=2)  # 允许同交易所/同模式最多绑定 API 数量
    multi_exchange_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 单笔下单上限(USDT),管理员强制下发,优先级高于客户自配的 RiskConfig
    max_order_usdt: Mapped[float] = mapped_column(default=5000.0)
    # ---- 急停开关 ----
    # True = 管理员一键阻断该客户的所有新开仓信号(平仓信号不受影响)
    # 借鉴 KOL 跟单系统的 emergency_stop 机制
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---- 客户分类 ----
    # normal(普通客户) | internal(内部用户)
    customer_type: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    # ---- 页面权限 ----
    # 是否在客户端开放“信号汇总”页面,默认隐藏,由管理员按用户开启
    show_signal_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---- 邀请系统 ----
    # 每个客户的唯一邀请码(注册时自动生成)
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, index=True, default=_generate_invite_code)
    # 邀请人ID(谁邀请的这个客户,NULL=无邀请人)
    invited_by: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)

    authorizations: Mapped[list["Authorization"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    # 邀请人关系(反向:被邀请的客户列表)
    invitees: Mapped[list["Customer"]] = relationship(
        "Customer", backref=backref("inviter", remote_side="Customer.id")
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

