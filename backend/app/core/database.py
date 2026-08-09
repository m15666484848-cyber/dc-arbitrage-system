"""异步数据库会话管理 (SQLAlchemy 2.0 async)。

引擎延迟创建:导入本模块不创建连接,首次调用 get_engine() 才创建。
这样纯逻辑测试与模型导入不依赖 asyncpg/数据库可用性。
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_engine() -> AsyncEngine:
    """延迟创建并返回异步引擎(单例)。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.is_dev,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_timeout=10,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    return _session_factory


# 兼容旧引用(在真正使用时才解析,不在导入期创建引擎)
class _EngineProxy:
    """代理对象,首次访问属性时才创建真实引擎。"""

    def __getattr__(self, name):
        return getattr(get_engine(), name)


engine = _EngineProxy()


def AsyncSessionLocal() -> AsyncSession:  # type: ignore[misc]
    """兼容旧调用:返回一个新会话。使用 `async with AsyncSessionLocal() as db:`。"""
    return get_session_factory()()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖:获取数据库会话,请求结束自动关闭。"""
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
