"""RSS cache persistence for the clean rebuild."""

from datetime import UTC, datetime, timedelta

from .db import get_db


def list_rss_items(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                show_id,
                season_number,
                episode_number,
                title,
                guid,
                size,
                pub_date,
                aw_episode_link,
                created_at
            FROM show_rss_cache
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_rss_items() -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM show_rss_cache")
    return int(cursor.rowcount or 0)


def has_rss_item(show_id: int, season_number: int, episode_number: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM show_rss_cache
            WHERE show_id = ? AND season_number = ? AND episode_number = ?
            LIMIT 1
            """,
            (show_id, season_number, episode_number),
        ).fetchone()
    return bool(row)


def save_rss_item(
    *,
    show_id: int,
    season_number: int,
    episode_number: int,
    title: str,
    guid: str,
    size: int,
    pub_date: str,
    aw_episode_link: str,
) -> bool:
    created_at = datetime.now(UTC).isoformat()
    with get_db(write=True) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO show_rss_cache (
                show_id,
                season_number,
                episode_number,
                title,
                guid,
                size,
                pub_date,
                aw_episode_link,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (show_id, season_number, episode_number, title, guid, size, pub_date, aw_episode_link, created_at),
        )
    return bool(cursor.rowcount)


def cleanup_rss_items(max_age_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    with get_db(write=True) as conn:
        cursor = conn.execute(
            """
            DELETE FROM show_rss_cache
            WHERE datetime(created_at) < datetime(?)
            """,
            (cutoff.isoformat(),),
        )
    return int(cursor.rowcount or 0)
