"""Repository helpers for sync metadata."""

import sqlite3

from .db import get_db


def get_sync_meta(key: str) -> str | None:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM sync_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None
    except sqlite3.Error:
        return None
