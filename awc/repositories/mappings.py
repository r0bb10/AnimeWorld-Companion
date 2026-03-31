"""Mapping repository for the clean rebuild."""

from .db import get_db


def count_show_mappings() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM aw_show_mappings").fetchone()
    return int(row["count"]) if row else 0


def count_movie_mappings() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM aw_movie_mappings").fetchone()
    return int(row["count"]) if row else 0


def recent_show_mappings(limit: int = 10) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                asm.show_id,
                asm.season_number,
                asm.part,
                asm.aw_link,
                asm.mapping_type,
                asm.confidence_score,
                s.title
            FROM aw_show_mappings asm
            JOIN shows s ON s.id = asm.show_id
            ORDER BY asm.updated_at DESC, asm.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_show_mappings(show_id: int, season_number: int | None = None) -> list[dict]:
    query = """
        SELECT
            id,
            show_id,
            season_number,
            part,
            aw_link,
            aw_title,
            aw_episode_count,
            aw_total_episodes,
            aw_status,
            aw_category,
            mapping_type,
            confidence_score,
            confidence_factors,
            linked_with_season,
            last_verified,
            updated_at
        FROM aw_show_mappings
        WHERE show_id = ?
    """
    params: list[object] = [show_id]
    if season_number is not None:
        query += " AND season_number = ?"
        params.append(season_number)
    query += " ORDER BY season_number, part, id"

    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def get_mapping_scenario(show_id: int, aw_link: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT season_number) AS season_count
            FROM aw_show_mappings
            WHERE show_id = ? AND aw_link = ?
            """,
            (show_id, aw_link),
        ).fetchone()
    season_count = row["season_count"] if row else 0
    return "single_link" if season_count > 1 else "normal_or_split"


def get_internal_episode(show_id: int, scene_season: int, scene_episode: int) -> tuple[int, int] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT internal_season, internal_episode
            FROM show_scene_episodes
            WHERE show_id = ? AND scene_season = ? AND scene_episode = ?
            """,
            (show_id, scene_season, scene_episode),
        ).fetchone()
    if not row:
        return None
    return row["internal_season"], row["internal_episode"]


def get_episode_by_absolute(show_id: int, absolute_episode: int) -> tuple[int, int] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT internal_season, internal_episode
            FROM show_scene_episodes
            WHERE show_id = ? AND absolute_episode = ?
            """,
            (show_id, absolute_episode),
        ).fetchone()
    if not row:
        return None
    return row["internal_season"], row["internal_episode"]
