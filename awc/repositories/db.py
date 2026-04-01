"""Database access boundary for the clean rebuild."""

from contextlib import contextmanager
import os
import sqlite3
import time

from ..core.config import settings


@contextmanager
def get_db(write: bool = False):
    db_path = settings.database_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = None
    last_error = None
    for attempt in range(5):
        try:
            conn = sqlite3.connect(db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
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
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.25 * (attempt + 1))
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
