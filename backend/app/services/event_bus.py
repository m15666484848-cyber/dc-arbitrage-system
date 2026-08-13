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
        self._dropped_count = 0  # P2-15: 丢弃事件计数器,监控队列溢出频率

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues[topic].add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        self._queues[topic].discard(q)
        # 清理空的 topic 集合,防止内存泄漏
        if not self._queues[topic]:
            del self._queues[topic]

    def clear(self) -> None:
        """清理所有订阅(用于优雅关闭或测试重置)。"""
        self._queues.clear()
        self._dropped_count = 0

    def clear_topic(self, topic: str) -> None:
        """清理指定 topic 的所有订阅。"""
        if topic in self._queues:
            del self._queues[topic]

    @property
    def dropped_count(self) -> int:
        """返回丢弃事件计数(用于监控)。"""
        return self._dropped_count

    async def publish(self, topic: str, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data}, default=str, ensure_ascii=False)
        for q in list(self._queues.get(topic, set())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # P2-15: 丢弃事件计数器 + ERROR 级别日志(含 customer_id)
                self._dropped_count += 1
                # 从 topic 提取 customer_id (topic 格式: customer:{id} 或 admin)
                customer_id = topic.split(":")[-1] if ":" in topic else topic
                logger.error(
                    f"客户频道队列满,丢弃事件 count={self._dropped_count} "
                    f"customer={customer_id} topic={topic} event={event}"
                )
        # 管理员总能看到
        if topic != "admin":
            for q in list(self._queues.get("admin", set())):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    self._dropped_count += 1
                    logger.error(
                        f"管理员频道队列满,丢弃事件 count={self._dropped_count} "
                        f"topic={topic} event={event}"
                    )

    async def publish_customer(self, customer_id: int, event: str, data: Any) -> None:
        await self.publish(f"customer:{customer_id}", event, data)


bus = EventBus()
