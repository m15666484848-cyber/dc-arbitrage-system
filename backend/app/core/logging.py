"""日志配置 (loguru)。"""
import re
import sys

from loguru import logger

from app.core.config import settings

# 敏感信息脱敏: 交易所 API Key / Secret / 密码 / Bearer Token 泄露到日志时统一打码
_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 请求头形式: X-BAPI-API-KEY': 'xxxx / OK-ACCESS-KEY: xxxx / api-key: xxxx
    (
        re.compile(
            r"((?:X-BAPI-API-KEY|OK-ACCESS-KEY|OK-ACCESS-SIGN|OK-ACCESS-PASSPHRASE|"
            r"X-API-KEY|API-KEY|APIKEY|api_key|apiKey|access_key|accessKey|secret|"
            r"secret_key|secretKey|passphrase|password|token)"
            r"['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9+/_.\-@!?#$%^&*]{3})[A-Za-z0-9+/_.\-@!?#$%^&*]*",
            re.IGNORECASE,
        ),
        r"\1\2****",
    ),
    # Bearer Token
    (
        re.compile(r"(Bearer\s+)([A-Za-z0-9_.\-]{3})[A-Za-z0-9_.\-]*"),
        r"\1\2****",
    ),
]


def _sanitize(text: str) -> str:
    for pattern, repl in _SENSITIVE_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _sensitive_patcher(record) -> None:
    msg = record.get("message")
    if isinstance(msg, str) and msg:
        record["message"] = _sanitize(msg)


def setup_logging() -> None:
    logger.remove()
    level = "DEBUG" if settings.is_dev else "INFO"
    logger.configure(patcher=_sensitive_patcher)
    logger.add(
        sys.stdout,
        level=level,
        colorize=True,
        # diagnose=False: 生产环境不显示 traceback 局部变量值,避免泄露 API Key/Secret
        backtrace=True,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )
    logger.add(
        "logs/app_{time:YYYYMMDD}.log",
        level=level,
        # diagnose=False: 生产环境不显示 traceback 局部变量值,避免泄露 API Key/Secret
        backtrace=True,
        diagnose=False,
        rotation="00:00",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )
