"""Write-side repositories for manager sync."""

from datetime import UTC, datetime

from .db import get_db
from .title_normalization import normalize_title


def _now() -> str:
    return datetime.now(UTC).isoformat()


def set_sync_meta(key: str, value: str) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO sync_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _now()),
        )


def upsert_show(payload: dict) -> int:
    with get_db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO shows (
                sonarr_id, tvdb_id, title, sort_title, series_type,
                monitored, status, year, original_language, first_aired,
                genres, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sonarr_id) DO UPDATE SET
                tvdb_id = excluded.tvdb_id,
                title = excluded.title,
                sort_title = excluded.sort_title,
                series_type = excluded.series_type,
                monitored = excluded.monitored,
                status = excluded.status,
                year = excluded.year,
                original_language = excluded.original_language,
                first_aired = excluded.first_aired,
                genres = excluded.genres,
                updated_at = excluded.updated_at
            """,
            (
                payload.get("sonarr_id"),
                payload.get("tvdb_id"),
                payload.get("title"),
                payload.get("sort_title"),
                payload.get("series_type", "standard"),
                int(bool(payload.get("monitored", True))),
                payload.get("status"),
                payload.get("year"),
                payload.get("original_language"),
                payload.get("first_aired"),
                payload.get("genres"),
                _now(),
            ),
        )
        row = conn.execute("SELECT id FROM shows WHERE sonarr_id = ?", (payload.get("sonarr_id"),)).fetchone()
    return int(row["id"])


def replace_show_seasons(show_id: int, seasons: list[dict]) -> None:
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM show_seasons WHERE show_id = ?", (show_id,))
        for season in seasons:
            conn.execute(
                """
                INSERT INTO show_seasons (
                    show_id, season_number, monitored, episode_count,
                    air_date_start, air_date_end, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    show_id,
                    season.get("season_number"),
                    int(bool(season.get("monitored", False))),
                    season.get("episode_count", 0),
                    season.get("air_date_start"),
                    season.get("air_date_end"),
                    _now(),
                ),
            )


def replace_show_alternate_titles(show_id: int, titles: list[dict]) -> None:
    seen: set[str] = set()
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM show_alternate_titles WHERE show_id = ?", (show_id,))
        for item in titles:
            title = item.get("title")
            if not title:
                continue
            normalized = normalize_title(title)
            if normalized in seen:
                continue
            seen.add(normalized)
            conn.execute(
                """
                INSERT INTO show_alternate_titles (
                    show_id, title, title_normalized, source,
                    title_type, language, scene_season_number, title_year
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    show_id,
                    title,
                    normalized,
                    item.get("source", "sonarr"),
                    item.get("title_type"),
                    item.get("language"),
                    item.get("scene_season_number"),
                    item.get("title_year"),
                ),
            )


def replace_scene_episode_map(show_id: int, items: list[dict]) -> None:
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM show_scene_episodes WHERE show_id = ?", (show_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO show_scene_episodes (
                    show_id, scene_season, scene_episode,
                    internal_season, internal_episode, absolute_episode, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    show_id,
                    item.get("scene_season"),
                    item.get("scene_episode"),
                    item.get("internal_season"),
                    item.get("internal_episode"),
                    item.get("absolute_episode"),
                    _now(),
                ),
            )


def prune_missing_shows(sonarr_ids: set[int]) -> int:
    if not sonarr_ids:
        return 0
    placeholders = ",".join("?" for _ in sonarr_ids)
    with get_db(write=True) as conn:
        cursor = conn.execute(f"DELETE FROM shows WHERE sonarr_id NOT IN ({placeholders})", tuple(sonarr_ids))
    return int(cursor.rowcount or 0)


def upsert_movie(payload: dict) -> int:
    with get_db(write=True) as conn:
        conn.execute(
            """
            INSERT INTO movies (
                radarr_id, tmdb_id, imdb_id, title, sort_title,
                monitored, status, year, original_language, first_aired,
                genres, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(radarr_id) DO UPDATE SET
                tmdb_id = excluded.tmdb_id,
                imdb_id = excluded.imdb_id,
                title = excluded.title,
                sort_title = excluded.sort_title,
                monitored = excluded.monitored,
                status = excluded.status,
                year = excluded.year,
                original_language = excluded.original_language,
                first_aired = excluded.first_aired,
                genres = excluded.genres,
                updated_at = excluded.updated_at
            """,
            (
                payload.get("radarr_id"),
                payload.get("tmdb_id"),
                payload.get("imdb_id"),
                payload.get("title"),
                payload.get("sort_title"),
                int(bool(payload.get("monitored", True))),
                payload.get("status"),
                payload.get("year"),
                payload.get("original_language"),
                payload.get("first_aired"),
                payload.get("genres"),
                _now(),
            ),
        )
        row = conn.execute("SELECT id FROM movies WHERE radarr_id = ?", (payload.get("radarr_id"),)).fetchone()
    return int(row["id"])


def replace_movie_alternate_titles(movie_id: int, titles: list[dict]) -> None:
    seen: set[str] = set()
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM movie_alternate_titles WHERE movie_id = ?", (movie_id,))
        for item in titles:
            title = item.get("title")
            if not title:
                continue
            normalized = normalize_title(title)
            if normalized in seen:
                continue
            seen.add(normalized)
            conn.execute(
                """
                INSERT INTO movie_alternate_titles (
                    movie_id, title, title_normalized, source, language
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    movie_id,
                    title,
                    normalized,
                    item.get("source", "radarr"),
                    item.get("language"),
                ),
            )


def prune_missing_movies(radarr_ids: set[int]) -> int:
    if not radarr_ids:
        return 0
    placeholders = ",".join("?" for _ in radarr_ids)
    with get_db(write=True) as conn:
        cursor = conn.execute(f"DELETE FROM movies WHERE radarr_id NOT IN ({placeholders})", tuple(radarr_ids))
    return int(cursor.rowcount or 0)
