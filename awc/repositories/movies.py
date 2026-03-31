"""Movie repository for the clean rebuild."""

import re

from .db import get_db


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    title = title.lower().strip()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def count_movies() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM movies").fetchone()
    return int(row["count"]) if row else 0


def list_movie_summaries(limit: int = 25) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.radarr_id,
                m.title,
                m.year,
                m.status,
                CASE WHEN amm.id IS NULL THEN 0 ELSE 1 END AS mapped
            FROM movies m
            LEFT JOIN aw_movie_mappings amm ON amm.movie_id = m.id
            ORDER BY m.title COLLATE NOCASE
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_movie_detail(movie_id: int) -> dict | None:
    with get_db() as conn:
        movie_row = conn.execute(
            """
            SELECT
                id,
                radarr_id,
                tmdb_id,
                imdb_id,
                title,
                sort_title,
                monitored,
                status,
                year,
                original_language,
                first_aired,
                genres
            FROM movies
            WHERE id = ?
            """,
            (movie_id,),
        ).fetchone()
        if not movie_row:
            return None

        alt_rows = conn.execute(
            """
            SELECT title, source, language
            FROM movie_alternate_titles
            WHERE movie_id = ?
            ORDER BY title COLLATE NOCASE
            """,
            (movie_id,),
        ).fetchall()

        mapping_row = conn.execute(
            """
            SELECT
                id,
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

    movie = dict(movie_row)
    movie["alternate_titles"] = [dict(row) for row in alt_rows]
    movie["mapping"] = dict(mapping_row) if mapping_row else None
    return movie


def find_movie_by_title(title: str) -> dict | None:
    normalized = _normalize_title(title)
    if not normalized:
        return None

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM movies
            WHERE lower(trim(title)) = ?
            LIMIT 1
            """,
            (title.lower().strip(),),
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT m.*
            FROM movies m
            JOIN movie_alternate_titles mat ON mat.movie_id = m.id
            WHERE mat.title_normalized = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT *
            FROM movies
            WHERE lower(title) LIKE ?
            ORDER BY length(title) ASC
            LIMIT 1
            """,
            (f"%{title.lower().strip()}%",),
        ).fetchone()
        return dict(row) if row else None


def find_movie_by_manager_identity(
    radarr_id: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str = "",
    title: str = "",
) -> dict | None:
    with get_db() as conn:
        if radarr_id is not None:
            row = conn.execute(
                """
                SELECT *
                FROM movies
                WHERE radarr_id = ?
                LIMIT 1
                """,
                (radarr_id,),
            ).fetchone()
            if row:
                return dict(row)

        if tmdb_id is not None:
            row = conn.execute(
                """
                SELECT *
                FROM movies
                WHERE tmdb_id = ?
                LIMIT 1
                """,
                (tmdb_id,),
            ).fetchone()
            if row:
                return dict(row)

        if imdb_id:
            row = conn.execute(
                """
                SELECT *
                FROM movies
                WHERE imdb_id = ?
                LIMIT 1
                """,
                (imdb_id,),
            ).fetchone()
            if row:
                return dict(row)

    return find_movie_by_title(title)


def find_movie_by_external_ids(tmdb_id: int | None = None, imdb_id: str = "") -> dict | None:
    with get_db() as conn:
        if tmdb_id is not None:
            row = conn.execute(
                """
                SELECT *
                FROM movies
                WHERE tmdb_id = ?
                LIMIT 1
                """,
                (tmdb_id,),
            ).fetchone()
            if row:
                return dict(row)

        if imdb_id:
            row = conn.execute(
                """
                SELECT *
                FROM movies
                WHERE imdb_id = ?
                LIMIT 1
                """,
                (imdb_id,),
            ).fetchone()
            if row:
                return dict(row)

    return None
