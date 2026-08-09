"""初始化管理员账号(首次部署运行)。"""
import asyncio
import os

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.security import hash_password
from app.models import User

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


async def init() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if not ADMIN_PASSWORD:
        raise RuntimeError("请通过 ADMIN_PASSWORD 环境变量设置管理员密码")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == settings.admin_username))
        existing = result.scalar_one_or_none()
        if existing:
            existing.password_hash = hash_password(ADMIN_PASSWORD)
            existing.is_active = True
            await db.commit()
            print(f"管理员 {settings.admin_username} 密码已重置")
        else:
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(ADMIN_PASSWORD),
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            print(f"管理员 {settings.admin_username} 创建成功")


if __name__ == "__main__":
    asyncio.run(init())
