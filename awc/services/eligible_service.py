"""Periodic mapping for newly eligible unmapped items."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

from ..core.config import settings
from ..core.log_events import format_movie_automap_lines, format_show_automap_lines, log_block
from ..core.logging import get_logger
from ..repositories.db import get_db
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .automap_service import automap_movie, automap_show

logger = get_logger("eligible_service")


def _lookback_sql() -> str:
    return f"-{max(0, int(settings.eligible_lookback_days or 0))} day"


def list_eligible_show_targets() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id AS show_id,
                s.title,
                ss.season_number,
                ss.air_date_start
            FROM shows s
            JOIN show_seasons ss ON ss.show_id = s.id
            LEFT JOIN aw_show_mappings asm
                ON asm.show_id = ss.show_id
               AND asm.season_number = ss.season_number
            WHERE ss.season_number > 0
              AND COALESCE(ss.ignored, 0) = 0
              AND asm.id IS NULL
              AND COALESCE(ss.air_date_start, '') != ''
              AND date(ss.air_date_start) >= date('now', ?)
            ORDER BY date(ss.air_date_start), lower(s.title), ss.season_number
            """,
            (_lookback_sql(),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_eligible_movie_targets() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id AS movie_id,
                m.title,
                m.first_aired
            FROM movies m
            LEFT JOIN aw_movie_mappings amm ON amm.movie_id = m.id
            WHERE COALESCE(m.ignored, 0) = 0
              AND amm.id IS NULL
              AND COALESCE(m.first_aired, '') != ''
              AND date(substr(m.first_aired, 1, 10)) >= date('now', ?)
            ORDER BY date(substr(m.first_aired, 1, 10)), lower(m.title)
            """,
            (_lookback_sql(),),
        ).fetchall()
    return [dict(row) for row in rows]


def _log_show_mapping(show_id: int, mapped_seasons: list[int]) -> None:
    if not mapped_seasons:
        return
    show = get_show_detail(show_id)
    if not show:
        return
    log_block(
        logger,
        logging.INFO,
        str(show.get("title") or f"show:{show_id}"),
        format_show_automap_lines(show, mapped_seasons, []),
    )


def _log_movie_mapping(movie_id: int) -> None:
    movie = get_movie_detail(movie_id)
    if not movie or not movie.get("mapping"):
        return
    log_block(
        logger,
        logging.INFO,
        str(movie.get("title") or f"movie:{movie_id}"),
        format_movie_automap_lines(movie.get("mapping")),
    )


def run_eligible_once() -> dict:
    show_targets = list_eligible_show_targets()
    movie_targets = list_eligible_movie_targets()
    checked = 0
    mapped_show_seasons = 0
    mapped_movies = 0

    logger.info(
        "Eligible cycle started: shows=%s movies=%s",
        len(show_targets),
        len(movie_targets),
    )

    for target in show_targets:
        checked += 1
        result = automap_show(
            int(target["show_id"]),
            season_number=int(target["season_number"]),
            force=False,
            emit_logs=False,
        )
        mapped = list(result.get("mapped_seasons") or [])
        if mapped:
            mapped_show_seasons += len(mapped)
            _log_show_mapping(int(target["show_id"]), mapped)

    for target in movie_targets:
        checked += 1
        result = automap_movie(int(target["movie_id"]), force=False, emit_logs=False)
        if result.get("status") == "success" and result.get("mapping"):
            mapped_movies += 1
            _log_movie_mapping(int(target["movie_id"]))

    result = {
        "checked": checked,
        "show_targets": len(show_targets),
        "movie_targets": len(movie_targets),
        "mapped_show_seasons": mapped_show_seasons,
        "mapped_movies": mapped_movies,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    logger.info(
        "Eligible cycle finished: checked=%s mapped_show_seasons=%s mapped_movies=%s",
        checked,
        mapped_show_seasons,
        mapped_movies,
    )
    return result
