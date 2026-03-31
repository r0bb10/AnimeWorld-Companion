"""RSS cache persistence for the clean rebuild."""

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
