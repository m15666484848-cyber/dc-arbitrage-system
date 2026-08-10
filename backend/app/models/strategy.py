"""策略配置模型:普通/马丁格尔/反马丁格尔。"""
from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin

# 策略类型
STRATEGY_NORMAL = "normal"
STRATEGY_MARTINGALE = "martingale"
STRATEGY_ANTI_MARTINGALE = "anti_martingale"


class Strategy(Base, TimestampMixin):
    """策略配置(可绑定到客户-KOL 跟随关系)。

    type: normal(普通) / martingale(马丁格尔) / anti_martingale(反马丁格尔)
    params: {
        base_qty: 基础仓位(USDT),
        martingale_multiplier: 倍数(默认2),
        max_rounds: 连亏/连胜上限熔断(默认3),
        tp_levels: [[price_pct, close_pct], ...]  # 止盈分级与平仓比例
        default_tp_pct: 缺失止盈时的默认(0.03, 0.05, 0.08)
        default_sl_pct: 缺失止损时的默认(-0.05)
        cost_protection_buffer: 成本保护缓冲(0.02 即2%)
        enable_trailing: 追踪止损
        trailing_callback: 回撤比例
        no_stop_loss: 无止损模式(高危)
    }
    """

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32), index=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 马丁格尔运行状态(每策略独立追踪)
    martingale_round: Mapped[int] = mapped_column(Integer, default=0)
    last_result: Mapped[str] = mapped_column(String(16), default="")  # win|loss|""
    last_qty: Mapped[float] = mapped_column(Float, default=0.0)
    # 按 KOL + 币种隔离的马丁状态。
    # key 示例: "5:BTC/USDT" -> {"round": 1, "last_result": "loss", "last_qty": 100.0}
    martingale_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
