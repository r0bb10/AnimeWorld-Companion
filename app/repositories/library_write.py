"""Write-side repositories for manager sync."""

from datetime import UTC, datetime
import json

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
        season_numbers = [int(season.get("season_number") or 0) for season in seasons if season.get("season_number") is not None]
        if season_numbers:
            placeholders = ",".join("?" for _ in season_numbers)
            conn.execute(
                f"DELETE FROM show_seasons WHERE show_id = ? AND season_number NOT IN ({placeholders})",
                (show_id, *season_numbers),
            )
        else:
            conn.execute("DELETE FROM show_seasons WHERE show_id = ?", (show_id,))
        for season in seasons:
            conn.execute(
                """
                INSERT INTO show_seasons (
                    show_id, season_number, monitored, episode_count,
                    air_date_start, air_date_end, segment_markers, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(show_id, season_number) DO UPDATE SET
                    monitored = excluded.monitored,
                    episode_count = excluded.episode_count,
                    air_date_start = excluded.air_date_start,
                    air_date_end = excluded.air_date_end,
                    segment_markers = excluded.segment_markers,
                    updated_at = excluded.updated_at
                """,
                (
                    show_id,
                    season.get("season_number"),
                    int(bool(season.get("monitored", False))),
                    season.get("episode_count", 0),
                    season.get("air_date_start"),
                    season.get("air_date_end"),
                    json.dumps(season.get("segment_markers", [])),
                    _now(),
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
                    title_type, language, title_year
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    show_id,
                    title,
                    normalized,
                    item.get("source", "sonarr"),
                    item.get("title_type"),
                    item.get("language"),
                    item.get("title_year"),
                ),
            )


def replace_show_episode_numbers(show_id: int, items: list[dict]) -> None:
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM show_episode_numbers WHERE show_id = ?", (show_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO show_episode_numbers (
                    show_id, internal_season, internal_episode, absolute_episode, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    show_id,
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


def delete_show_by_sonarr_id(sonarr_id: int) -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM shows WHERE sonarr_id = ?", (sonarr_id,))
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


def delete_movie_by_radarr_id(radarr_id: int) -> int:
    with get_db(write=True) as conn:
        cursor = conn.execute("DELETE FROM movies WHERE radarr_id = ?", (radarr_id,))
    return int(cursor.rowcount or 0)
