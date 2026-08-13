"""客户级品种倍率覆盖模型。

每个客户可以覆盖管理员设置的分类倍率,也可以添加完全自定义的币种。
最终倍率优先级: 客户自定义币种 > 客户分类覆盖 > 管理员默认 > 1.0
"""

from sqlalchemy import text, Index, Numeric, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class CustomerSymbolMultiplier(Base, TimestampMixin):
    """客户对某个品种分类的倍率覆盖,或完全自定义的币种倍率。

    config_id 为 NULL 时,custom_symbol 字段指定自定义的币种前缀(如 "SKHY")。
    config_id 有值时,custom_symbol 为空,表示覆盖管理员预设分类的倍率。
    """

    __tablename__ = "customer_symbol_multipliers"
    __table_args__ = (
        UniqueConstraint("customer_id", "config_id", name="uq_customer_config"),
        Index("uq_customer_symbol_not_null", "customer_id", "custom_symbol", unique=True, postgresql_where=text("custom_symbol IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    config_id: Mapped[int | None] = mapped_column(ForeignKey("symbol_notional_configs.id", ondelete="CASCADE"), nullable=True, index=True)
    custom_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    multiplier: Mapped[float] = mapped_column(default=1.0)
