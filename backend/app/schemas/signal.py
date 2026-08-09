"""信号 schemas。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ParsedSignal(BaseModel):
    """解析后的结构化信号。"""

    # 标准化动作列表,支持一条消息里同时表达多个动作。
    # 常用取值:
    # - open_long / open_short: 明确新开多/开空
    # - close_position: 平仓/离场
    # - update_tp_sl: 更新止盈止损
    # - cancel_order: 撤销未成交挂单
    # - hold_pending: 仅说明旧挂单仍挂着,不代表新开仓
    # - refresh_pending: 延续/刷新旧挂单;若无旧挂单则按当前参数新挂
    actions: list[str] = []
    action: str = ""  # 兼容单动作字段,默认取 actions[0]
    symbol: str = ""
    side: str = ""  # long|short
    entry_price: float | None = None
    entry_prices: list[float] = []  # 分批入场价
    take_profits: list[float] = []  # 多级止盈
    stop_loss: float | None = None
    condition_price: float | None = None
    leverage: int = 1
    position_pct: float = 0.0
    raw_text: str = ""
    confidence: float = 0.0
    has_image: bool = False
    dedup_full_hash: str = ""  # 全周期去重指纹
    is_exit_signal: bool = False  # 是否为平仓信号
    exit_reason: str = ""  # 平仓原因
    is_update_signal: bool = False  # 是否为止盈止损更新信号
    update_reason: str = ""  # 更新原因(日志/通知)
    reason: str = ""  # 解析说明/忽略原因


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

    model_config = ConfigDict(from_attributes = True)


class SignalInjectRequest(BaseModel):
    """模拟注入信号(测试/手动信号)。"""

    kol_id: int
    raw_text: str = ""
    image_url: str = ""
