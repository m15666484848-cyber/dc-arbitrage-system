"""进程内事件总线:用于后台服务向 WebSocket 客户端推送实时事件。

每个客户订阅自身频道(topic = customer:{id});管理员订阅 admin 频道。
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from loguru import logger


class EventBus:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._queues[topic].discard(q)

    async def publish(self, topic: str, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data}, default=str, ensure_ascii=False)
        for q in list(self._queues.get(topic, set())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(f"event bus queue full, dropping event for {topic}")
        # 管理员总能看到
        if topic != "admin":
            for q in list(self._queues.get("admin", set())):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    async def publish_customer(self, customer_id: int, event: str, data: Any) -> None:
        await self.publish(f"customer:{customer_id}", event, data)


bus = EventBus()
