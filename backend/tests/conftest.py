"""Pytest 全局配置 — 自动 mock notify 防止测试发送真实告警"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_notify(monkeypatch):
    """自动 mock 所有测试的 notify 函数，防止发送真实飞书告警"""
    from app.services import order_manager, position_manager, notification

    async def fake_notify(*args, **kwargs):
        pass

    monkeypatch.setattr(notification, "notify", fake_notify)
    monkeypatch.setattr(order_manager, "notify", fake_notify)
    if hasattr(position_manager, "notify"):
        monkeypatch.setattr(position_manager, "notify", fake_notify)


@pytest.fixture(autouse=True)
def mock_event_bus(monkeypatch):
    """自动 mock event bus 防止测试发布真实事件"""
    from app.services import order_manager

    fake_bus = MagicMock()
    fake_bus.publish_customer = AsyncMock()
    fake_bus.publish = AsyncMock()
    monkeypatch.setattr(order_manager, "bus", fake_bus)
