
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.customer import Customer
from app.models.kol import KolFollow
from app.models.trading import Position


async def main():
    async with AsyncSessionLocal() as db:
        customer = (await db.execute(select(Customer).where(Customer.username == "kol_ui_test"))).scalar_one()
        follow = (await db.execute(select(KolFollow).where(KolFollow.customer_id == customer.id).limit(1))).scalar_one()
        follow.enabled = True
        follow.paused_until = None
        follow.cooldown_reset_at = None
        pos = Position(
            customer_id=customer.id,
            kol_id=follow.kol_id,
            parent_id=None,
            batch_no=1,
            exchange="okx",
            symbol="BTC/USDT",
            side="long",
            entry_price=60000.0,
            qty=0.001,
            initial_qty=0.001,
            tp_levels=[],
            sl=None,
            initial_sl=None,
            leverage=1,
            cost_protection=False,
            breakeven_moved=False,
            trailing_stop=False,
            trailing_callback=0.0,
            status="closed",
            realized_pnl=0.0,
            entry_fee=0.0,
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
        )
        db.add(pos)
        await db.commit()
        print({"customer_id": customer.id, "kol_id": follow.kol_id, "position_id": pos.id, "symbol": pos.symbol})


asyncio.run(main())
