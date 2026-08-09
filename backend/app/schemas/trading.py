"""交易(订单/持仓/成交)schemas。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class OrderOut(BaseModel):
    id: int
    customer_id: int
    kol_id: int | None = None
    kol_name: str = ""
    signal_id: int | None = None
    position_id: int | None = None
    exchange_account_id: int | None = None
    exchange: str
    symbol: str
    side: str
    type: str
    qty: float
    price: float | None = None
    leverage: int = 1
    batch_no: int = 1
    status: str
    exchange_order_id: str = ""
    filled_qty: float = 0.0
    filled_price: float = 0.0
    error_msg: str = ""
    tp_level: int = 0
    created_at: datetime
    filled_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PositionOut(BaseModel):
    id: int
    customer_id: int
    kol_id: int | None = None
    kol_name: str = ""
    exchange_account_id: int | None = None
    exchange: str
    symbol: str
    side: str
    entry_price: float
    qty: float
    initial_qty: float
    tp_levels: list[dict[str, Any]] = []
    sl: float | None = None
    leverage: int = 1
    cost_protection: bool = False
    breakeven_moved: bool = False
    trailing_stop: bool = False
    status: str
    realized_pnl: float = 0.0
    # 实时计算字段
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    pnl_pct: float = 0.0
    opened_at: datetime
    closed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TradeOut(BaseModel):
    id: int
    customer_id: int
    kol_id: int | None = None
    kol_name: str = ""
    position_id: int | None = None
    exchange_account_id: int | None = None
    exchange: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float = 0.0
    realized_pnl: float = 0.0
    is_close: bool = False
    tp_level: int = 0
    executed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClosePositionRequest(BaseModel):
    """手动平仓。"""

    position_id: int
    qty: float | None = None  # None=全部平仓;否则部分平仓


class ManualOrderRequest(BaseModel):
    """手动下单(客户自主下单,非跟单)。"""

    exchange: str
    symbol: str
    side: str  # buy|sell
    type: str = "market"  # market|limit
    qty: float
    price: float | None = None
    leverage: int = 1
    take_profits: list[float] = []
    stop_loss: float | None = None


class DeleteOrderRequest(BaseModel):
    """删除未成交挂单。"""

    order_id: int


class UpdateStopRequest(BaseModel):
    """手动修改止损(含成本保护/追踪止损)。"""

    position_id: int
    sl: float | None = None
    trailing_stop: bool | None = None
    trailing_callback: float | None = None
