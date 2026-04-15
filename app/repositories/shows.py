"""Show repository for the clean rebuild."""

import json

from .db import get_db
from .title_normalization import normalize_title


def count_shows() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM shows").fetchone()
    return int(row["count"]) if row else 0


def list_show_summaries(limit: int | None = 25) -> list[dict]:
    with get_db() as conn:
        if limit is None:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.sonarr_id,
                    s.title,
                    s.year,
                    s.status,
                    s.series_type,
                    COUNT(DISTINCT ss.id) AS season_count,
                    COUNT(DISTINCT asm.id) AS mapping_count
                FROM shows s
                LEFT JOIN show_seasons ss ON ss.show_id = s.id
                LEFT JOIN aw_show_mappings asm ON asm.show_id = s.id
                GROUP BY s.id
                ORDER BY s.title COLLATE NOCASE
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.sonarr_id,
                    s.title,
                    s.year,
                    s.status,
                    s.series_type,
                    COUNT(DISTINCT ss.id) AS season_count,
                    COUNT(DISTINCT asm.id) AS mapping_count
                FROM shows s
                LEFT JOIN show_seasons ss ON ss.show_id = s.id
                LEFT JOIN aw_show_mappings asm ON asm.show_id = s.id
                GROUP BY s.id
                ORDER BY s.title COLLATE NOCASE
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_show_detail(show_id: int) -> dict | None:
    with get_db() as conn:
        show_row = conn.execute(
            """
            SELECT
                id,
                sonarr_id,
                tvdb_id,
                title,
                sort_title,
                series_type,
                monitored,
                status,
                year,
                original_language,
                first_aired,
                genres
            FROM shows
            WHERE id = ?
            """,
            (show_id,),
        ).fetchone()
        if not show_row:
            return None

        alt_rows = conn.execute(
            """
            SELECT title, source, title_type, language, title_year
            FROM show_alternate_titles
            WHERE show_id = ?
            ORDER BY title COLLATE NOCASE
            """,
            (show_id,),
        ).fetchall()

        season_rows = conn.execute(
            """
            SELECT
                id,
                season_number,
                monitored,
                episode_count,
                air_date_start,
                air_date_end,
                segment_markers,
                ignored
            FROM show_seasons
            WHERE show_id = ?
            ORDER BY season_number
            """,
            (show_id,),
        ).fetchall()

        mapping_rows = conn.execute(
            """
            SELECT
                id,
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
            ORDER BY season_number, part, id
            """,
            (show_id,),
        ).fetchall()

    show = dict(show_row)
    show["alternate_titles"] = [dict(row) for row in alt_rows]

    mappings_by_season: dict[int, list[dict]] = {}
    for row in mapping_rows:
        item = dict(row)
        mappings_by_season.setdefault(item["season_number"], []).append(item)

    seasons = []
    for row in season_rows:
        season = dict(row)
        try:
            season["segment_markers"] = json.loads(season.get("segment_markers") or "[]")
        except (TypeError, ValueError):
            season["segment_markers"] = []
        season["mappings"] = mappings_by_season.get(season["season_number"], [])
        seasons.append(season)

    show["seasons"] = seasons
    return show


def find_show_by_title(title: str) -> dict | None:
    normalized = normalize_title(title)
    if not normalized:
        return None

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT s.*
            FROM shows s
            WHERE lower(trim(s.title)) = ?
            LIMIT 1
            """,
            (title.lower().strip(),),
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT s.*
            FROM shows s
            JOIN show_alternate_titles sat ON sat.show_id = s.id
            WHERE sat.title_normalized = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT s.*
            FROM shows s
            WHERE lower(s.title) LIKE ?
            ORDER BY length(s.title) ASC
            LIMIT 1
            """,
            (f"%{title.lower().strip()}%",),
        ).fetchone()
        return dict(row) if row else None


def find_show_by_manager_identity(sonarr_id: int | None = None, tvdb_id: int | None = None, title: str = "") -> dict | None:
    with get_db() as conn:
        if sonarr_id is not None:
            row = conn.execute(
                """
                SELECT *
                FROM shows
                WHERE sonarr_id = ?
                LIMIT 1
                """,
                (sonarr_id,),
            ).fetchone()
            if row:
                return dict(row)

        if tvdb_id is not None:
            row = conn.execute(
                """
                SELECT *
                FROM shows
                WHERE tvdb_id = ?
                LIMIT 1
                """,
                (tvdb_id,),
            ).fetchone()
            if row:
                return dict(row)

    return find_show_by_title(title)


def find_show_by_tvdb_id(tvdb_id: int | None) -> dict | None:
    if tvdb_id is None:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM shows
            WHERE tvdb_id = ?
            LIMIT 1
            """,
            (tvdb_id,),
        ).fetchone()
    return dict(row) if row else None



def set_season_ignored(show_id: int, season_number: int, ignored: bool) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute(
            """
            UPDATE show_seasons
            SET ignored = ?, updated_at = CURRENT_TIMESTAMP
            WHERE show_id = ? AND season_number = ?
            """,
            (1 if ignored else 0, show_id, season_number),
        )
    return bool(cursor.rowcount)


def delete_show(show_id: int) -> bool:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    return bool(cursor.rowcount)
