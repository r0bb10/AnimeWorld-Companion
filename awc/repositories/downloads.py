"""Download persistence for the clean rebuild."""

from datetime import datetime
from uuid import uuid4

from .db import get_db


def list_downloads(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                url,
                filename,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id
            FROM downloads
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_download(download_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                url,
                filename,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id
            FROM downloads
            WHERE id = ?
            LIMIT 1
            """,
            (download_id,),
        ).fetchone()
    return dict(row) if row else None


def create_download(
    *,
    url: str,
    filename: str,
    status: str,
    part_path: str,
    sonarr_id: int | None = None,
) -> dict:
    download_id = uuid4().hex
    created_at = datetime.now().timestamp()
    with get_db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO downloads (
                url,
                id,
                filename,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id
            )
            VALUES (?, ?, ?, ?, 0, 0, ?, NULL, NULL, NULL, ?, ?)
            """,
            (url, download_id, filename, status, part_path, created_at, sonarr_id),
        )
    return get_download(download_id) or {}


def update_download_status(download_id: str, status: str, error: str | None = None) -> dict | None:
    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE downloads
            SET status = ?, error = ?
            WHERE id = ?
            """,
            (status, error, download_id),
        )
    return get_download(download_id)


def delete_download(download_id: str) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
    return cursor.rowcount > 0


def clear_finished_downloads() -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute(
            """
            DELETE FROM downloads
            WHERE status IN ('imported', 'completed', 'failed', 'cancelled', 'removed')
            """
        )
    return int(cursor.rowcount or 0)
