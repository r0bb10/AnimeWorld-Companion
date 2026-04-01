"""RSS cache persistence for the clean rebuild."""

from datetime import UTC, datetime, timedelta

from .db import get_db


def list_rss_items(limit: int = 100) -> list[dict]:
    query_limit = max(limit, 1)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM (
                SELECT
                    id,
                    show_id,
                    NULL AS movie_id,
                    season_number,
                    episode_number,
                    title,
                    guid,
                    size,
                    pub_date,
                    aw_episode_link,
                    created_at,
                    '5070' AS category_id
                FROM show_rss_cache
                UNION ALL
                SELECT
                    id,
                    NULL AS show_id,
                    movie_id,
                    NULL AS season_number,
                    NULL AS episode_number,
                    title,
                    guid,
                    size,
                    pub_date,
                    aw_episode_link,
                    created_at,
                    '2000' AS category_id
                FROM movie_rss_cache
            )
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (query_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_rss_items() -> int:
    with get_db(write=True) as conn:
        removed_shows = conn.execute("DELETE FROM show_rss_cache").rowcount or 0
        removed_movies = conn.execute("DELETE FROM movie_rss_cache").rowcount or 0
    return int(removed_shows + removed_movies)


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


def has_movie_rss_item(movie_id: int, guid: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM movie_rss_cache
            WHERE movie_id = ? AND guid = ?
            LIMIT 1
            """,
            (movie_id, guid),
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


def save_movie_rss_item(
    *,
    movie_id: int,
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
            INSERT OR IGNORE INTO movie_rss_cache (
                movie_id,
                title,
                guid,
                size,
                pub_date,
                aw_episode_link,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (movie_id, title, guid, size, pub_date, aw_episode_link, created_at),
        )
    return bool(cursor.rowcount)


def cleanup_rss_items(max_age_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    with get_db(write=True) as conn:
        removed_shows = conn.execute(
            """
            DELETE FROM show_rss_cache
            WHERE datetime(created_at) < datetime(?)
            """,
            (cutoff.isoformat(),),
        ).rowcount or 0
        removed_movies = conn.execute(
            """
            DELETE FROM movie_rss_cache
            WHERE datetime(created_at) < datetime(?)
            """,
            (cutoff.isoformat(),),
        ).rowcount or 0
    return int(removed_shows + removed_movies)
