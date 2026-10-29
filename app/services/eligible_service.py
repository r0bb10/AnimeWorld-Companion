"""Periodic mapping for newly eligible unmapped items."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

from ..core.config import settings
from ..core.log_events import format_movie_automap_lines, format_show_automap_lines, log_animeworld_error, log_block, log_info
from ..core.logging import get_logger
from ..integrations.animeworld_access import AnimeWorldAccessError
from ..integrations.animeworld_transport import animeworld_operation
from ..repositories.db import get_db
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .automap_service import automap_movie, automap_show

logger = get_logger("eligible_service")
_DATELESS_SHOW_LIMIT = 10


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


def list_eligible_dateless_show_targets(limit: int = _DATELESS_SHOW_LIMIT) -> list[dict]:
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
              AND COALESCE(ss.air_date_start, '') = ''
              AND lower(COALESCE(s.status, '')) IN ('upcoming', 'continuing')
            ORDER BY lower(s.title), ss.season_number
            LIMIT ?
            """,
            (max(0, int(limit)),),
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
        event_type="eligible.show.mapped",
        entity_kind="show",
        entity_id=show_id,
        entity_title=show.get("title"),
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
        event_type="eligible.movie.mapped",
        entity_kind="movie",
        entity_id=movie_id,
        entity_title=movie.get("title"),
    )


@animeworld_operation("eligible")
def run_eligible_once() -> dict:
    dated_show_targets = list_eligible_show_targets()
    dateless_show_targets = list_eligible_dateless_show_targets()
    show_targets = dated_show_targets + dateless_show_targets
    movie_targets = list_eligible_movie_targets()
    checked = 0
    mapped_show_seasons = 0
    mapped_movies = 0
    failures: list[dict] = []
    access_error = None
    current_target = None

    log_info(
        logger,
        "eligible.started",
        "Eligible cycle started",
        lines=[
            f"shows={len(show_targets)}",
            f"dated_shows={len(dated_show_targets)}",
            f"dateless_shows={len(dateless_show_targets)}",
            f"movies={len(movie_targets)}",
        ],
        details={
            "show_targets": len(show_targets),
            "dated_show_targets": len(dated_show_targets),
            "dateless_show_targets": len(dateless_show_targets),
            "movie_targets": len(movie_targets),
        },
    )

    targets = [("show", target) for target in show_targets] + [("movie", target) for target in movie_targets]
    for kind, target in targets:
        current_target = target
        try:
            if kind == "show":
                item_result = automap_show(
                    int(target["show_id"]), season_number=int(target["season_number"]),
                    force=False, emit_logs=False,
                )
            else:
                item_result = automap_movie(int(target["movie_id"]), force=False, emit_logs=False)
        except AnimeWorldAccessError as exc:
            if exc.blocks_requests:
                access_error = exc
                break
            failure = {"target_kind": kind, "target": target, "error": str(exc), "upstream": exc.details()}
            failures.append(failure)
            log_animeworld_error(
                logger, "eligible.item.failed", "Eligible item failed", exc, details=failure,
                lines=[f"item={target.get('title', '')}"],
            )
        else:
            if kind == "show":
                mapped = list(item_result.get("mapped_seasons") or [])
                if mapped:
                    mapped_show_seasons += len(mapped)
                    _log_show_mapping(int(target["show_id"]), mapped)
            elif item_result.get("status") == "success" and item_result.get("mapping"):
                mapped_movies += 1
                _log_movie_mapping(int(target["movie_id"]))
        checked += 1

    result = {
        "status": "interrupted" if access_error else "partial" if failures else "completed",
        "checked": checked,
        "failed": len(failures),
        "failures": failures,
        "remaining": len(targets) - checked,
        "show_targets": len(show_targets),
        "dated_show_targets": len(dated_show_targets),
        "dateless_show_targets": len(dateless_show_targets),
        "movie_targets": len(movie_targets),
        "mapped_show_seasons": mapped_show_seasons,
        "mapped_movies": mapped_movies,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    if access_error:
        result["error"] = str(access_error)
        result["upstream"] = access_error.details()
        result["current_target"] = current_target
        log_animeworld_error(
            logger, "eligible.interrupted", "Eligible cycle interrupted", access_error, details=result,
            lines=[f"checked={checked}", f"remaining={result['remaining']}", f"item={(current_target or {}).get('title', '')}"],
        )
        return result
    if failures:
        result["error"] = f"Eligible cycle completed with {len(failures)} failed targets"
    log_info(
        logger,
        "eligible.finished",
        "Eligible cycle completed with failures" if failures else "Eligible cycle finished",
        lines=[
            f"checked={checked}",
            f"failed={len(failures)}",
            f"mapped_show_seasons={mapped_show_seasons}",
            f"mapped_movies={mapped_movies}",
        ],
        details=result,
    )
    return result
