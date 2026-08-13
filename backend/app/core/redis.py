"""Redis 客户端(用于信号去重窗口、实时状态、限流、缓存)。"""
import asyncio

from redis.asyncio import Redis, from_url

from app.core.config import settings

_redis: Redis | None = None
_redis_lock = asyncio.Lock()


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        # M5修复: 使用锁保护单例创建,防止并发竞态导致连接池泄漏
        async with _redis_lock:
            if _redis is None:  # double-check
                _redis = from_url(settings.redis_url, decode_responses=True, health_check_interval=30, retry_on_timeout=True, socket_timeout=5, socket_connect_timeout=5)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None
