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
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            timeout=30,
            uri=True,
        )
    conn.row_factory = sqlite3.Row
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
