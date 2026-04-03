"""Persistent structured log history store."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
import sqlite3
import threading

from ..core.config import settings

_LOG_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    level         TEXT NOT NULL,
    logger        TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    message       TEXT NOT NULL,
    lines_json    TEXT DEFAULT '[]',
    details_json  TEXT DEFAULT '{}',
    entity_kind   TEXT,
    entity_id     TEXT,
    entity_title  TEXT,
    traceback     TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_log_events_created_at ON log_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_events_level      ON log_events(level);
CREATE INDEX IF NOT EXISTS idx_log_events_logger     ON log_events(logger);
CREATE INDEX IF NOT EXISTS idx_log_events_type       ON log_events(event_type);
CREATE INDEX IF NOT EXISTS idx_log_events_entity     ON log_events(entity_kind, entity_id);
"""


def _db_path() -> str:
    return settings.log_db_path


@contextmanager
def get_log_db(write: bool = False):
    db_path = _db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with _LOG_LOCK:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.row_factory = sqlite3.Row
            if write:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("PRAGMA query_only = 1")
            try:
                yield conn
                if write:
                    conn.commit()
            except Exception:
                if write:
                    conn.rollback()
                raise
        finally:
            conn.close()


def init_log_db() -> None:
    with get_log_db(write=True) as conn:
        conn.executescript(_SCHEMA)


def prune_log_events(retention_days: int) -> int:
    days = max(0, int(retention_days or 0))
    if days <= 0:
        return 0
    with get_log_db(write=True) as conn:
        cursor = conn.execute(
            "DELETE FROM log_events WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{days} day",),
        )
    return int(cursor.rowcount or 0)


def insert_log_event(
    *,
    created_at: str,
    level: str,
    logger: str,
    event_type: str,
    message: str,
    lines: list[str] | None = None,
    details: dict | None = None,
    entity_kind: str | None = None,
    entity_id: str | None = None,
    entity_title: str | None = None,
    traceback: str = "",
) -> None:
    payload_lines = json.dumps(list(lines or []), default=str)
    payload_details = json.dumps(dict(details or {}), default=str)
    with get_log_db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO log_events (
                created_at,
                level,
                logger,
                event_type,
                message,
                lines_json,
                details_json,
                entity_kind,
                entity_id,
                entity_title,
                traceback
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at or datetime.now(UTC).isoformat(),
                level,
                logger,
                event_type,
                message,
                payload_lines,
                payload_details,
                entity_kind,
                entity_id,
                entity_title,
                traceback or "",
            ),
        )


def query_log_events(
    *,
    level: str = "",
    logger_name: str = "",
    event_type: str = "",
    since: str = "",
    until: str = "",
    q: str = "",
    limit: int = 200,
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if level:
        clauses.append("level = ?")
        params.append(level.upper())
    if logger_name:
        clauses.append("logger = ?")
        params.append(logger_name)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since:
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(since)
    if until:
        clauses.append("datetime(created_at) <= datetime(?)")
        params.append(until)
    if q:
        clauses.append("(message LIKE ? OR details_json LIKE ? OR traceback LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    bounded_limit = max(1, min(int(limit or 200), 1000))
    with get_log_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                created_at,
                level,
                logger,
                event_type,
                message,
                lines_json,
                details_json,
                entity_kind,
                entity_id,
                entity_title,
                traceback
            FROM log_events
            {where}
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (*params, bounded_limit),
        ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        try:
            item["lines"] = json.loads(item.pop("lines_json") or "[]")
        except (TypeError, ValueError):
            item["lines"] = []
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except (TypeError, ValueError):
            item["details"] = {}
        results.append(item)
    return results
