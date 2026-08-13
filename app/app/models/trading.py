"""订单、持仓、成交记录模型。"""
from datetime import datetime

from sqlalchemy import Numeric, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class Order(Base, TimestampMixin):
    """订单:每次下单的记录(含分批建仓的子单)。"""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kol_id: Mapped[int | None] = mapped_column(ForeignKey("kols.id", ondelete="SET NULL"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    exchange_account_id: Mapped[int | None] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)  # okx|binance|bybit
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(16))  # buy|sell
    type: Mapped[str] = mapped_column(String(16))  # market|limit
    # 审计字段:系统计划下单名义价值(USDT),用于事后核对策略金额与实际成交
    notional_usdt: Mapped[float] = mapped_column(default=0.0)
    # 审计字段:entry(开仓/补仓) / close(普通平仓) / tp_close(止盈平仓) / sl_close(止损平仓) / unknown(历史订单)
    order_role: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    qty: Mapped[float] = mapped_column(Numeric)
    price: Mapped[float | None] = mapped_column(nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    batch_no: Mapped[int] = mapped_column(Integer, default=1)  # 分批建仓序号
    # status: pending(挂单中) / filled(已成交) / partial(部分成交) / cancelled(已撤) / deleted(已删) / failed(失败)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(128), default="")
    filled_qty: Mapped[float] = mapped_column(default=0.0)
    filled_price: Mapped[float] = mapped_column(default=0.0)
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 软删除
    tp_level: Mapped[int] = mapped_column(Integer, default=0)  # 平仓时表示第几止盈(0=建仓单)


class Position(Base, TimestampMixin):
    """持仓:支持主仓位(物理聚合)与子仓位(KOL虚拟隔离)的分层模型。"""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kol_id: Mapped[int | None] = mapped_column(ForeignKey("kols.id", ondelete="SET NULL"), nullable=True, index=True)
    # 关键:父仓位ID。主仓位(物理)此字段为空，子仓位(虚拟)指向主仓位ID。
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"), nullable=True, index=True)
    batch_no: Mapped[int] = mapped_column(Integer, default=1)  # 分批建仓序号
    exchange_account_id: Mapped[int | None] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(16))  # long|short
    entry_price: Mapped[float] = mapped_column(Numeric)
    qty: Mapped[float] = mapped_column(Numeric)  # 剩余持仓量
    initial_qty: Mapped[float] = mapped_column(Numeric)  # 初始建仓量
    # 多级止盈 [{level:1, price:.., pct:0.3, status:'pending|hit'}]
    tp_levels: Mapped[list] = mapped_column(JSONB, default=list)
    sl: Mapped[float | None] = mapped_column(nullable=True)  # 当前止损价
    initial_sl: Mapped[float | None] = mapped_column(nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    # 成本保护:达到 TP1 或 +2% 后止损上移至入场价+缓冲
    cost_protection: Mapped[bool] = mapped_column(Boolean, default=False)
    breakeven_moved: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_callback: Mapped[float] = mapped_column(default=0.0)  # 回撤比例
    tp_sl_source: Mapped[str] = mapped_column(String(16), default="kol")  # kol|default|timeout
    # status: open(持仓中) / closed(已平仓) / liquidated(强平)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    realized_pnl: Mapped[float] = mapped_column(default=0.0)
    entry_fee: Mapped[float] = mapped_column(default=0.0)  # 开仓手续费(USDT),用于平仓时计算净盈亏
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())


class Trade(Base, TimestampMixin):
    """成交流水:用于交易记录、统计与账户走势。"""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    kol_id: Mapped[int | None] = mapped_column(ForeignKey("kols.id", ondelete="SET NULL"), nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    exchange_account_id: Mapped[int | None] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(16))  # buy|sell
    qty: Mapped[float] = mapped_column(Numeric)
    price: Mapped[float] = mapped_column(Numeric)
    fee: Mapped[float] = mapped_column(default=0.0)
    realized_pnl: Mapped[float] = mapped_column(default=0.0)  # 平仓成交时为正负盈亏
    is_close: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否平仓成交
    tp_level: Mapped[int] = mapped_column(Integer, default=0)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())
