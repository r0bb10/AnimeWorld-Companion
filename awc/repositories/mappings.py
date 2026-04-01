"""Mapping repository for the clean rebuild."""

from datetime import UTC, datetime

from ..integrations.animeworld_client import AnimeWorldClient
from .db import get_db


def _normalize_aw_link(value: str) -> str:
    return AnimeWorldClient().url_to_slug((value or "").strip())


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
    aw_link = _normalize_aw_link(aw_link)
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


def replace_show_mapping(
    *,
    show_id: int,
    season_number: int,
    aw_link: str,
    aw_title: str = "",
    part: int = 1,
    aw_episode_count: int = 0,
    aw_total_episodes: int = 0,
    aw_status: str = "",
    aw_category: str = "",
    linked_with_season: int | None = None,
) -> list[dict]:
    aw_link = _normalize_aw_link(aw_link)
    now = datetime.now(UTC).isoformat()
    with get_db(write=True) as conn:
        conn.execute(
            "DELETE FROM aw_show_mappings WHERE show_id = ? AND season_number = ?",
            (show_id, season_number),
        )
        conn.execute(
            """
            INSERT INTO aw_show_mappings (
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
                last_verified,
                created_at,
                updated_at,
                linked_with_season
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 1.0, NULL, ?, ?, ?, ?)
            """,
            (
                show_id,
                season_number,
                part,
                aw_link,
                aw_title,
                aw_episode_count,
                aw_total_episodes,
                aw_status,
                aw_category,
                now,
                now,
                now,
                linked_with_season,
            ),
        )
    return list_show_mappings(show_id, season_number)


def remove_show_mapping(show_id: int, season_number: int) -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute(
            "DELETE FROM aw_show_mappings WHERE show_id = ? AND season_number = ?",
            (show_id, season_number),
        )
    return int(cursor.rowcount or 0)


def replace_movie_mapping(
    *,
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
) -> dict | None:
    aw_link = _normalize_aw_link(aw_link)
    now = datetime.now(UTC).isoformat()
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM aw_movie_mappings WHERE movie_id = ?", (movie_id,))
        conn.execute(
            """
            INSERT INTO aw_movie_mappings (
                movie_id,
                aw_link,
                aw_title,
                aw_status,
                aw_category,
                mapping_type,
                confidence_score,
                confidence_factors,
                link_check_failures,
                last_verified,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'manual', 1.0, NULL, 0, ?, ?, ?)
            """,
            (movie_id, aw_link, aw_title, aw_status, aw_category, now, now, now),
        )
        row = conn.execute(
            """
            SELECT
                id,
                movie_id,
                aw_link,
                aw_title,
                aw_status,
                aw_category,
                mapping_type,
                confidence_score,
                confidence_factors,
                last_verified,
                updated_at
            FROM aw_movie_mappings
            WHERE movie_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (movie_id,),
        ).fetchone()
    return dict(row) if row else None


def remove_movie_mapping(movie_id: int) -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM aw_movie_mappings WHERE movie_id = ?", (movie_id,))
    return int(cursor.rowcount or 0)


def replace_show_mappings_auto(
    *,
    show_id: int,
    season_number: int,
    items: list[dict],
) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    with get_db(write=True) as conn:
        conn.execute(
            "DELETE FROM aw_show_mappings WHERE show_id = ? AND season_number = ?",
            (show_id, season_number),
        )
        for item in items:
            conn.execute(
                """
                INSERT INTO aw_show_mappings (
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
                    last_verified,
                    created_at,
                    updated_at,
                    linked_with_season
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?)
                """,
                (
                    show_id,
                    season_number,
                    item.get("part", 1),
                    _normalize_aw_link(item.get("aw_link", "")),
                    item.get("aw_title", ""),
                    item.get("aw_episode_count", 0),
                    item.get("aw_total_episodes", 0),
                    item.get("aw_status", ""),
                    item.get("aw_category", ""),
                    item.get("confidence_score", 0.0),
                    item.get("confidence_factors"),
                    now,
                    now,
                    now,
                    item.get("linked_with_season"),
                ),
            )
    return list_show_mappings(show_id, season_number)


def replace_movie_mapping_auto(
    *,
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
    confidence_score: float = 0.0,
    confidence_factors: str | None = None,
) -> dict | None:
    aw_link = _normalize_aw_link(aw_link)
    now = datetime.now(UTC).isoformat()
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM aw_movie_mappings WHERE movie_id = ?", (movie_id,))
        conn.execute(
            """
            INSERT INTO aw_movie_mappings (
                movie_id,
                aw_link,
                aw_title,
                aw_status,
                aw_category,
                mapping_type,
                confidence_score,
                confidence_factors,
                link_check_failures,
                last_verified,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, 0, ?, ?, ?)
            """,
            (movie_id, aw_link, aw_title, aw_status, aw_category, confidence_score, confidence_factors, now, now, now),
        )
        row = conn.execute(
            """
            SELECT
                id,
                movie_id,
                aw_link,
                aw_title,
                aw_status,
                aw_category,
                mapping_type,
                confidence_score,
                confidence_factors,
                last_verified,
                updated_at
            FROM aw_movie_mappings
            WHERE movie_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (movie_id,),
        ).fetchone()
    return dict(row) if row else None
