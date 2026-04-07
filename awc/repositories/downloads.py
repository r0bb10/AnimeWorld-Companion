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
                release_source,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id,
                radarr_id
            FROM downloads
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_downloads() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                url,
                filename,
                release_source,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id,
                radarr_id
            FROM downloads
            ORDER BY created_at DESC
            """
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
                release_source,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id,
                radarr_id
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
    release_source: str = "unknown",
    status: str,
    part_path: str,
    sonarr_id: int | None = None,
    radarr_id: int | None = None,
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
                release_source,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id,
                radarr_id
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                url,
                download_id,
                filename,
                release_source,
                status,
                part_path,
                created_at,
                sonarr_id,
                radarr_id,
            ),
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


def update_download_progress(
    download_id: str,
    *,
    status: str | None = None,
    total_bytes: int | None = None,
    downloaded_bytes: int | None = None,
    part_path: str | None = None,
    error: str | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
) -> dict | None:
    current = get_download(download_id)
    if not current:
        return None
    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE downloads
            SET
                status = ?,
                total_bytes = ?,
                downloaded_bytes = ?,
                part_path = ?,
                error = ?,
                started_at = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                status if status is not None else current["status"],
                total_bytes if total_bytes is not None else current["total_bytes"],
                downloaded_bytes if downloaded_bytes is not None else current["downloaded_bytes"],
                part_path if part_path is not None else current["part_path"],
                error if error is not None else current["error"],
                started_at if started_at is not None else current["started_at"],
                finished_at if finished_at is not None else current["finished_at"],
                download_id,
            ),
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
            WHERE status IN ('imported', 'failed', 'cancelled', 'removed', 'vanished')
            """
        )
    return int(cursor.rowcount or 0)


def list_completed_downloads() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                url,
                filename,
                release_source,
                status,
                total_bytes,
                downloaded_bytes,
                part_path,
                error,
                started_at,
                finished_at,
                created_at,
                sonarr_id,
                radarr_id
            FROM downloads
            WHERE status = 'completed'
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
