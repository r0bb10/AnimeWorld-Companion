"""Logging helpers for the clean rebuild."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings

_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_DEBUG_ONLY_UVICORN_MESSAGES = (
    "Started server process",
    "Waiting for application startup.",
    "Application startup complete.",
    "Waiting for connections to close.",
    "Uvicorn running on ",
)


class RecordRewriteFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error" and _resolve_level() > logging.DEBUG:
            message = record.getMessage()
            if any(message.startswith(prefix) for prefix in _DEBUG_ONLY_UVICORN_MESSAGES):
                return False
        if record.name == "uvicorn.access":
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


class TimezoneFormatter(logging.Formatter):
    def __init__(self, fmt: str, timezone_name: str) -> None:
        super().__init__(fmt=fmt)
        try:
            self._tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self._tz = UTC

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self._tz)
        return dt.strftime(datefmt or "%Y/%m/%d %H:%M:%S")


def _resolve_level() -> int:
    level_name = (settings.log_level or "INFO").upper()
    if level_name not in _VALID_LEVELS:
        level_name = "INFO"
    return getattr(logging, level_name, logging.INFO)


def configure_logging() -> None:
    level = _resolve_level()
    formatter = TimezoneFormatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        timezone_name=settings.timezone_name,
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(RecordRewriteFilter())
    root.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        if logger_name == "uvicorn.access" and level > logging.DEBUG:
            uvicorn_logger.setLevel(logging.WARNING)
        else:
            uvicorn_logger.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
