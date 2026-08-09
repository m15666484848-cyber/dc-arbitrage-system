"""策略 schemas。"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from app.models.strategy import (
    STRATEGY_ANTI_MARTINGALE,
    STRATEGY_MARTINGALE,
    STRATEGY_NORMAL,
)

VALID_STRATEGY_TYPES = {STRATEGY_NORMAL, STRATEGY_MARTINGALE, STRATEGY_ANTI_MARTINGALE}


class StrategyParams(BaseModel):
    """策略参数。"""

    base_qty: float = 100.0  # 基础仓位(USDT)
    martingale_multiplier: float = 2.0  # 马丁倍数
    max_rounds: int = 3  # 熔断轮数
    tp_levels: list[list[float]] = []  # [[涨幅, 平仓比例], ...]
    default_tp_pct: list[float] = [0.10, 0.20]  # 缺失止盈默认
    default_sl_pct: float = -0.05  # 缺失止损默认
    cost_protection_buffer: float = 0.002  # 成本保护缓冲
    enable_trailing: bool = False
    trailing_callback: float = 0.01
    no_stop_loss: bool = False  # 高危
    batch_entry_enabled: bool = True  # 分批建仓
    batch_entry_window: int = 300  # 分批建仓窗口(秒)


class StrategyOut(BaseModel):
    id: int
    customer_id: int
    name: str
    type: str
    params: dict[str, Any] = {}
    martingale_round: int = 0
    last_result: str = ""
    last_qty: float = 0.0
    enabled: bool
    created_at: datetime

    class Config:
        from_attributes = True


class StrategyCreate(BaseModel):
    name: str
    type: str
    params: StrategyParams = StrategyParams()
    enabled: bool = True

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_STRATEGY_TYPES:
            raise ValueError(f"策略类型 '{v}' 不合法, 可选值: {sorted(VALID_STRATEGY_TYPES)}")
        return v
