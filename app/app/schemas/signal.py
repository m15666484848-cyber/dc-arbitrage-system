"""信号 schemas。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ParsedSignal(BaseModel):
    """解析后的结构化信号。"""

    symbol: str = ""
    side: str = ""  # long|short
    entry_price: float | None = None
    entry_prices: list[float] = []  # 分批入场价
    take_profits: list[float] = []  # 多级止盈
    stop_loss: float | None = None
    leverage: int = 1
    position_pct: float = 0.0
    raw_text: str = ""
    confidence: float = 0.0
    has_image: bool = False
    dedup_full_hash: str = ""  # 全周期去重指纹
    is_exit_signal: bool = False  # 是否为平仓信号
    exit_reason: str = ""  # 平仓原因


class SignalOut(BaseModel):
    id: int
    kol_id: int
    kol_name: str = ""
    raw_text: str = ""
    image_url: str = ""
    parsed: dict[str, Any] = {}
    status: str
    dedup_hash: str = ""
    corrected: bool = False
    correct_log: str = ""
    confidence: float = 0.0
    symbol: str = ""
    side: str = ""
    entry_price: float | None = None
    received_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class SignalInjectRequest(BaseModel):
    """模拟注入信号(测试/手动信号)。"""

    kol_id: int
    raw_text: str = ""
    image_url: str = ""
