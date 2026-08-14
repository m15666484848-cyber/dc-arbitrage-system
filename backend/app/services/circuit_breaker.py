"""
交易所API断路器 (Circuit Breaker)

在交易所API连续失败时自动断开调用,防止1秒止损监控循环被超时拖垮。
断路器打开期间使用缓存价格进行止损检查,降级但不中断监控。

状态机:
  CLOSED   → 正常调用交易所API
  OPEN     → 连续失败达阈值,暂停API调用,使用缓存价格
  HALF_OPEN → 恢复期过后允许一次试探性调用
"""
from __future__ import annotations

import time
from enum import Enum
from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """交易所API断路器。

    Usage:
        cb = CircuitBreaker(name="fetch_prices", threshold=3, recovery_time=60)
        if cb.can_call():
            try:
                result = await exchange_api_call()
                cb.record_success()
            except Exception:
                cb.record_failure()
                result = None  # 降级到缓存
        else:
            result = None  # 断路器打开,使用缓存
    """

    def __init__(
        self,
        name: str = "default",
        threshold: int = 3,
        recovery_time: float = 60.0,
    ):
        self.name = name
        self.threshold = threshold
        self.recovery_time = recovery_time
        self._fail_count = 0
        self._last_fail_time = 0.0
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        return self._state

    def can_call(self) -> bool:
        """是否允许调用交易所API。"""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            # 检查恢复时间是否已过
            if time.monotonic() - self._last_fail_time > self.recovery_time:
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    f"[断路器:{self.name}] OPEN → HALF_OPEN, "
                    f"尝试恢复 (失败次数={self._fail_count})"
                )
                return True
            return False
        # HALF_OPEN: 只允许一次试探
        return True

    def record_success(self) -> None:
        """记录API调用成功,重置断路器。"""
        if self._state != CircuitState.CLOSED:
            logger.info(
                f"[断路器:{self.name}] {self._state.value} → CLOSED, 恢复正常"
            )
        self._fail_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """记录API调用失败,连续达阈值则打开断路器。"""
        self._fail_count += 1
        self._last_fail_time = time.monotonic()
        if self._fail_count >= self.threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    f"[断路器:{self.name}] {self._state.value} → OPEN, "
                    f"连续失败 {self._fail_count} 次,暂停API调用 {self.recovery_time}s"
                )
            self._state = CircuitState.OPEN
        else:
            logger.debug(
                f"[断路器:{self.name}] 失败 {self._fail_count}/{self.threshold}"
            )

    def reset(self) -> None:
        """手动重置断路器。"""
        self._fail_count = 0
        self._state = CircuitState.CLOSED


# 全局断路器实例: 按交易所名称隔离
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, threshold: int = 3, recovery_time: float = 60.0) -> CircuitBreaker:
    """获取或创建指定名称的断路器实例。"""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name=name, threshold=threshold, recovery_time=recovery_time
        )
    return _breakers[name]


def get_all_breaker_status() -> dict:
    """获取所有断路器状态(供健康检查使用)。"""
    return {
        name: {
            "state": b.state.value,
            "fail_count": b._fail_count,
            "threshold": b.threshold,
            "recovery_time": b.recovery_time,
        }
        for name, b in _breakers.items()
    }
