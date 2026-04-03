"""Logging helpers for the clean rebuild."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings
from ..repositories.log_history import init_log_db, insert_log_event, prune_log_events

_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_DEBUG_ONLY_UVICORN_MESSAGES = (
    "Started server process",
    "Waiting for application startup.",
    "Application startup complete.",
    "Waiting for connections to close.",
    "Uvicorn running on ",
)
_BLOCK_MARKER = "↳"
_QUEUE_LISTENER: QueueListener | None = None


class SQLiteLogHandler(logging.Handler):
    def __init__(self, level: int) -> None:
        super().__init__(level=level)
        self._formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            traceback = ""
            if record.exc_info:
                traceback = self._formatter.formatException(record.exc_info)
            insert_log_event(
                created_at=datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                level=str(record.levelname or "INFO"),
                logger=str(record.name or ""),
                event_type=str(getattr(record, "awc_event_type", "log")),
                message=str(record.getMessage()),
                lines=list(getattr(record, "awc_lines", []) or []),
                details=dict(getattr(record, "awc_details", {}) or {}),
                entity_kind=str(getattr(record, "awc_entity_kind", "") or "") or None,
                entity_id=str(getattr(record, "awc_entity_id", "") or "") or None,
                entity_title=str(getattr(record, "awc_entity_title", "") or "") or None,
                traceback=traceback,
            )
        except Exception:
            self.handleError(record)


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

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        lines = [str(line) for line in (getattr(record, "awc_lines", []) or []) if str(line).strip()]
        if not lines:
            return rendered
        message = str(record.getMessage() or "")
        prefix = ""
        if message:
            marker_index = rendered.find(message)
            if marker_index >= 0:
                prefix = rendered[:marker_index]
        continuation_indent = " " * len(prefix)
        continuation = "\n".join(f"{continuation_indent}  {_BLOCK_MARKER} {line}" for line in lines)
        return f"{rendered}\n{continuation}"


def _resolve_level() -> int:
    level_name = (settings.log_level or "INFO").upper()
    if level_name not in _VALID_LEVELS:
        level_name = "INFO"
    return getattr(logging, level_name, logging.INFO)


def configure_logging() -> None:
    global _QUEUE_LISTENER
    level = _resolve_level()
    formatter = TimezoneFormatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        timezone_name=settings.timezone_name,
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if _QUEUE_LISTENER is not None:
        _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(RecordRewriteFilter())
    root.addHandler(handler)

    if settings.log_db_enabled:
        init_log_db()
        prune_log_events(settings.log_db_retention_days)
        queue: SimpleQueue = SimpleQueue()
        queue_handler = QueueHandler(queue)
        queue_handler.setLevel(level)
        queue_handler.addFilter(RecordRewriteFilter())
        root.addHandler(queue_handler)
        sqlite_handler = SQLiteLogHandler(level=level)
        _QUEUE_LISTENER = QueueListener(queue, sqlite_handler, respect_handler_level=True)
        _QUEUE_LISTENER.start()

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


def shutdown_logging() -> None:
    global _QUEUE_LISTENER
    if _QUEUE_LISTENER is not None:
        _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None
