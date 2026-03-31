"""Database access boundary for the clean rebuild."""

from contextlib import contextmanager
import os
import sqlite3

from ..core.config import settings


@contextmanager
def get_db(write: bool = False):
    db_path = settings.database_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    if write:
        conn = sqlite3.connect(db_path, timeout=30)
    else:
        conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    if not write:
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
