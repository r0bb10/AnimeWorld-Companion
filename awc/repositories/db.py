"""Database access boundary for the clean rebuild."""

from contextlib import contextmanager
import os
import sqlite3

from ..core.config import settings


@contextmanager
def get_db():
    db_path = settings.database_path
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro&immutable=1",
        timeout=30,
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception:
        raise
    finally:
        conn.close()
