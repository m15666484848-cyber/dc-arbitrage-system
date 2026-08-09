"""日志配置 (loguru)。"""
import sys

from loguru import logger

from app.core.config import settings


def setup_logging() -> None:
    logger.remove()
    level = "DEBUG" if settings.is_dev else "INFO"
    logger.add(
        sys.stdout,
        level=level,
        colorize=True,
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
        rotation="00:00",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )
