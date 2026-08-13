"""策略 schemas。"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.models.strategy import (
    STRATEGY_ANTI_MARTINGALE,
    STRATEGY_MARTINGALE,
    STRATEGY_NORMAL,
)

VALID_STRATEGY_TYPES = {STRATEGY_NORMAL, STRATEGY_MARTINGALE, STRATEGY_ANTI_MARTINGALE}


class StrategyParams(BaseModel):
    """策略参数。"""

    base_qty: float = Field(100.0, ge=0, description="基础仓位(USDT)")
    martingale_multiplier: float = Field(2.0, ge=1, description="马丁倍数")
    max_rounds: int = Field(3, ge=1, le=20, description="熔断轮数")
    # 止盈分级: None=按币种分层自动分配, 简化格式 [10, 20, 30] 或旧格式 [[0.1,0.3],...]
    tp_levels: list | None = None
    default_tp_pct: list[float] | None = None  # None=分层模式自动分配
    default_sl_pct: float | None = None  # None=按币种分层默认
    cost_protection_buffer: float = Field(0.02, ge=0, le=0.1, description="成本保护缓冲")
    enable_trailing: bool = False
    trailing_callback: float = Field(0.01, ge=0, le=0.5, description="回撤比例")
    no_stop_loss: bool = False  # 高危
    batch_entry_enabled: bool = True  # 分批建仓
    batch_entry_window: int = Field(300, ge=0, le=3600, description="分批建仓窗口(秒)")
    # 币种分层默认止盈止损
    use_tiered_defaults: bool = True
    # 硬止损上限(None或0=按币种分层默认 8/12/20%)
    max_sl_pct: float | None = None
    # 超时分级保护
    timeout_protection_enabled: bool = True
    timeout_phase1_hours: int = Field(4, ge=0, description="超时阶段1小时")
    timeout_phase2_hours: int = Field(24, ge=0, description="超时阶段2小时")
    timeout_phase3_hours: int = Field(72, ge=0, description="超时阶段3小时")
    timeout_phase4_hours: int = Field(96, ge=0, description="超时阶段4小时")
    timeout_trailing_p1: float = Field(0.03, ge=0, le=0.5, description="超时回撤P1")
    timeout_trailing_p2: float = Field(0.02, ge=0, le=0.5, description="超时回撤P2")


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

    model_config = ConfigDict(from_attributes=True)


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
