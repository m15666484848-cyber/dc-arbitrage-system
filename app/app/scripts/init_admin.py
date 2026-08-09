"""初始化管理员账号(首次部署运行)。"""
import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.security import hash_password
from app.models import User


async def init() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == settings.admin_username))
        if result.scalar_one_or_none():
            print(f"管理员 {settings.admin_username} 已存在,跳过")
            return
        admin = User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            is_active=True,
        )
        db.add(admin)
        await db.commit()
        print(f"管理员 {settings.admin_username} 创建成功")


if __name__ == "__main__":
    asyncio.run(init())
