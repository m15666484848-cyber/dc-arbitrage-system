
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.customer import Customer
from app.models.kol import Kol, KolFollow


async def main():
    username = "kol_ui_test"
    password = "KolUiTest@123"
    async with AsyncSessionLocal() as db:
        customer = (await db.execute(select(Customer).where(Customer.username == username))).scalar_one_or_none()
        if not customer:
            customer = Customer(
                username=username,
                password_hash=hash_password(password),
                display_name="KOL状态前端验证",
                is_active=True,
                status="active",
                register_source="admin",
            )
            db.add(customer)
            await db.flush()
        else:
            customer.password_hash = hash_password(password)
            customer.display_name = "KOL状态前端验证"
            customer.is_active = True
            customer.status = "active"

        kol = (await db.execute(select(Kol).where(Kol.enabled.is_(True)).order_by(Kol.id).limit(1))).scalar_one_or_none()
        if not kol:
            kol = Kol(
                name="UI测试KOL",
                discord_channel_id="ui-test-channel",
                discord_user_id="ui-test-user",
                enabled=True,
                description="前端状态验证用KOL",
            )
            db.add(kol)
            await db.flush()

        follow = (await db.execute(
            select(KolFollow).where(KolFollow.customer_id == customer.id, KolFollow.kol_id == kol.id)
        )).scalar_one_or_none()
        if not follow:
            follow = KolFollow(customer_id=customer.id, kol_id=kol.id, enabled=False, followed_notional_usdt=100.0)
            db.add(follow)

        follow.enabled = False
        follow.paused_until = datetime.now(timezone.utc) + timedelta(hours=2)
        follow.cooldown_reset_at = None
        follow.followed_notional_usdt = 100.0
        await db.commit()
        print({
            "username": username,
            "password": password,
            "customer_id": customer.id,
            "kol_id": kol.id,
            "kol_name": kol.name,
            "paused_until": follow.paused_until.isoformat(),
        })


asyncio.run(main())
