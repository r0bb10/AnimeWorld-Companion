"""Database access boundary for the clean rebuild."""

from contextlib import contextmanager
import os
import sqlite3
import time
import threading

from ..core.config import settings

_WRITE_LOCK = threading.RLock()


@contextmanager
def get_db(write: bool = False):
    db_path = settings.database_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    lock = _WRITE_LOCK if write else None
    if lock is not None:
        lock.acquire()

    conn = None
    last_error = None
    try:
        for attempt in range(10):
            try:
                conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
                conn.execute("PRAGMA foreign_keys=ON")
                if write:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.row_factory = sqlite3.Row
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    conn.execute("PRAGMA query_only = 1")
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if conn is not None:
                    conn.close()
                    conn = None
                if "locked" not in str(exc).lower() or attempt == 9:
                    raise
                time.sleep(min(0.1 * (attempt + 1), 1.0))
        if conn is None:
            raise last_error or RuntimeError("unable to open database connection")
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
    finally:
        if lock is not None:
            lock.release()
