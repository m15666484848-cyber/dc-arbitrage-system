"""分类倍率配置模型:按品种分类设置跟单金额倍率。

分类示例:
- 主流币: BTC, ETH → 倍率 0.5 (波动小,下单量减半)
- 贵金属: XAU, XAG → 倍率 1.0
- 能源: XTI, XBR → 倍率 1.0
- 山寨币: SOL, DOGE, PEPE → 倍率 2.0 (波动大,但本金风险高)
"""

from sqlalchemy import Float, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class SymbolNotionalConfig(Base, TimestampMixin):
    """按 symbol 前缀分类设置下单倍率。

    匹配规则: symbol 以 prefix 开头(不区分大小写)时应用 multiplier。
    例如 prefix="BTC" 会匹配 BTC/USDT、BTCUSDT 等。
    若一个 symbol 匹配多个分类,取第一个匹配。
    """

    __tablename__ = "symbol_notional_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    symbols: Mapped[str] = mapped_column(String(500), default="")
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(200), default="")

    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]
