"""Mapped AnimeWorld link verification and refresh."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import json
import logging
import threading

import requests

from ..core.config import settings
from ..core.log_events import (
    display_aw_link,
    format_movie_automap_lines,
    format_show_automap_lines,
    log_block,
    log_info,
    log_warning,
)
from ..core.logging import get_logger
from ..domain.mapping_flags import factors_are_preaired, mapping_is_preaired, mapping_is_preaired_placeholder
from ..domain.release_window import has_started
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
from ..repositories.mappings import (
    clear_movie_sanitizer_retry,
    clear_show_sanitizer_retry,
    list_movie_sanitizer_retries,
    list_show_sanitizer_retries,
    queue_movie_sanitizer_retry,
    queue_show_sanitizer_retry,
)
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .automap_language import resolve_movie_language_preference, resolve_show_language_preference
from .automap_scoring import calculate_movie_confidence, calculate_show_confidence, parse_italian_date
from .events_service import publish_library_card_changed, publish_library_stats_changed

_state_lock = threading.Lock()
logger = get_logger("sanitizer")
_state = {
    "enabled": settings.sanitizer_enabled,
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": "",
    "last_result": None,
}
_NETWORK_WORKERS = 6


class _Soft404MappingError(RuntimeError):
    """Raised when a mapped AnimeWorld slug resolves to a not-found page."""


def sanitizer_status() -> dict:
    with _state_lock:
        return dict(_state)


def _set_state(**updates) -> None:
    with _state_lock:
        _state.update(updates)


def _verify_slug(client: AnimeWorldClient, slug: str) -> dict:
    return dict(client.verify_slug_details(slug))


def _is_transient_aw_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            return True
        return int(status) >= 500 or int(status) == 429
    if isinstance(exc, requests.RequestException):
        return True
    return False


def _has_aired(season: dict) -> bool:
    return has_started((season or {}).get("air_date_start"))


def _season_by_number(show: dict, season_number: int) -> dict | None:
    return next(
        (
            item
            for item in (show or {}).get("seasons", [])
            if int(item.get("season_number") or 0) == int(season_number)
        ),
        None,
    )


def _candidate_from_slug_metadata(
    *,
    metadata: dict,
    title: str,
    status: str,
    category: str,
    year: int | None,
) -> dict:
    release_value = str(metadata.get("release_value") or "")
    release_dt = parse_italian_date(release_value) if release_value else None
    candidate = {
        "aw_link": str(metadata.get("slug") or ""),
        "aw_title": title,
        "aw_jtitle": "",
        "aw_status": str(metadata.get("status") or status or ""),
        "aw_category": str(metadata.get("category") or category or ""),
        "aw_audio": str(metadata.get("audio") or ""),
        "aw_year": release_dt.year if release_dt else year,
        "aw_release_datetime": release_dt,
        "aw_episode_count": int(metadata.get("non_special") or 0),
        "aw_total_episodes": int(metadata.get("total") or 0),
        "aw_is_placeholder": bool(metadata.get("is_placeholder")),
    }
    return candidate


def _row_is_preaired(row: dict) -> bool:
    return mapping_is_preaired(row)


def _row_is_preaired_placeholder(row: dict) -> bool:
    return mapping_is_preaired_placeholder(row)


def _touch_show_mapping(row_id: int, now: str) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_show_mappings
            SET link_check_failures = 0,
                last_verified = ?
            WHERE id = ?
            """,
            (now, row_id),
        )


def _touch_movie_mapping(row_id: int, now: str) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_movie_mappings
            SET link_check_failures = 0,
                last_verified = ?
            WHERE id = ?
            """,
            (now, row_id),
        )


def _publish_library_change(kind: str, item_id: int) -> None:
    publish_library_card_changed(kind, item_id)
    publish_library_stats_changed()


def _refresh_show_mapping_metadata(show: dict, season: dict, row: dict, slug_metadata: dict, now: str) -> bool:
    candidate = _candidate_from_slug_metadata(
        metadata=slug_metadata,
        title=str(row.get("aw_title") or show.get("title") or ""),
        status=str(row.get("aw_status") or ""),
        category=str(row.get("aw_category") or ""),
        year=show.get("year"),
    )
    season_payload = {**season, "has_aired": _has_aired(season)}
    want_dubbed = resolve_show_language_preference(show)
    score, factors = calculate_show_confidence(show, season_payload, candidate, want_dubbed=want_dubbed)
    if bool(factors.get("preaired_placeholder")):
        factors["preaired"] = True
        factors["preaired_type"] = "placeholder"
    elif _row_is_preaired(row) and not _has_aired(season) and not candidate.get("aw_is_placeholder"):
        factors["preaired"] = True
        factors["preaired_type"] = "prereleased"
        factors["preaired_prereleased"] = True

    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_show_mappings
            SET aw_link = ?,
                aw_status = ?,
                aw_category = ?,
                aw_episode_count = ?,
                aw_total_episodes = ?,
                confidence_score = ?,
                confidence_factors = ?,
                link_check_failures = 0,
                last_verified = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                candidate["aw_link"],
                candidate["aw_status"],
                candidate["aw_category"],
                candidate["aw_episode_count"],
                candidate["aw_total_episodes"],
                score,
                json.dumps(factors),
                now,
                now,
                row["id"],
            ),
        )
    was_pre = _row_is_preaired(row)
    is_pre = factors_are_preaired(factors)
    changed = candidate["aw_link"] != row["aw_link"] or was_pre != is_pre
    if changed:
        _publish_library_change("show", int(row["show_id"]))
        lines = format_show_automap_lines(show, [int(row["season_number"])])
        if candidate["aw_link"] != row["aw_link"]:
            lines.append(
                f"redirect={display_aw_link(row['aw_link'])} -> {display_aw_link(candidate['aw_link'])}"
            )
        if was_pre and not is_pre:
            lines.append("promoted=pre -> auto")
        log_block(
            logger,
            logging.INFO,
            str(show.get("title") or row.get("aw_title") or "Show"),
            lines,
            event_type="sanitizer.show.changed",
            entity_kind="show",
            entity_id=row.get("show_id"),
            entity_title=str(show.get("title") or row.get("aw_title") or "Show"),
        )
    return changed


def _refresh_movie_mapping_metadata(movie: dict, row: dict, slug_metadata: dict, now: str) -> bool:
    candidate = _candidate_from_slug_metadata(
        metadata=slug_metadata,
        title=str(row.get("aw_title") or movie.get("title") or ""),
        status=str(row.get("aw_status") or ""),
        category=str(row.get("aw_category") or ""),
        year=movie.get("year"),
    )
    want_dubbed = resolve_movie_language_preference(movie)
    score, factors = calculate_movie_confidence(movie, candidate, want_dubbed=want_dubbed)

    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_movie_mappings
            SET aw_link = ?,
                aw_status = ?,
                aw_category = ?,
                confidence_score = ?,
                confidence_factors = ?,
                link_check_failures = 0,
                last_verified = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                candidate["aw_link"],
                candidate["aw_status"],
                candidate["aw_category"],
                score,
                json.dumps(factors),
                now,
                now,
                row["id"],
            ),
        )
    changed = candidate["aw_link"] != row["aw_link"]
    if changed:
        _publish_library_change("movie", int(row["movie_id"]))
        lines = format_movie_automap_lines(
            {
                "aw_link": candidate["aw_link"],
                "confidence_score": score,
            }
        )
        log_block(
            logger,
            logging.INFO,
            str(movie.get("title") or row.get("aw_title") or "Movie"),
            lines
            + [f"redirect={display_aw_link(row['aw_link'])} -> {display_aw_link(candidate['aw_link'])}"]
            if candidate["aw_link"] != row["aw_link"]
            else lines,
            event_type="sanitizer.movie.changed",
            entity_kind="movie",
            entity_id=row.get("movie_id"),
            entity_title=str(movie.get("title") or row.get("aw_title") or "Movie"),
        )
    return changed


def _fetch_slug_verification(slug: str) -> dict:
    client = AnimeWorldClient()
    return _verify_slug(client, slug)


def _fetch_slug_metadata(slug: str) -> dict:
    client = AnimeWorldClient()
    info, episodes, _, is_placeholder = client.get_info_and_episodes_meta(slug)
    non_special, total, _ = client.count_non_special_episodes(episodes)
    return {
        "slug": slug,
        "status": str(info.get("Stato") or ""),
        "category": str(info.get("Categoria") or ""),
        "audio": str(info.get("Audio") or ""),
        "release_value": str(info.get("Data di Uscita") or ""),
        "non_special": non_special,
        "total": total,
        "is_placeholder": is_placeholder,
    }


def _needs_show_mapping_refresh(show: dict, season: dict) -> bool:
    mappings = list(season.get("mappings") or [])
    if not mappings:
        return False
    if any((mapping.get("mapping_type") or "") != "auto" for mapping in mappings):
        return False

    markers = [dict(item) for item in (season.get("segment_markers") or []) if int(item.get("count") or 0) > 0]
    if len(markers) <= 1:
        return False

    ordered_mappings = sorted(mappings, key=lambda item: int(item.get("part") or 0))
    marker_counts = [int(item.get("count") or 0) for item in markers]
    mapping_counts = [int(item.get("aw_episode_count") or 0) for item in ordered_mappings]
    season_total = int(season.get("episode_count") or 0)

    # A single link remains valid when it covers Sonarr's full season, even if
    # Sonarr later adds segment markers. Refresh only when it covers the first
    # segment but no longer the complete season.
    if len(ordered_mappings) <= 1:
        mapping_count = mapping_counts[0] if mapping_counts else 0
        return (
            mapping_count > 0
            and abs(mapping_count - marker_counts[0]) <= 1
            and season_total > mapping_count + 1
        )

    if len(marker_counts) != len(mapping_counts):
        return True
    if any(abs(left - right) > 1 for left, right in zip(marker_counts, mapping_counts)):
        return True

    if season_total and abs(sum(mapping_counts) - season_total) > 1:
        return True
    return False


def sanitize_links_once() -> dict:
    client = AnimeWorldClient()
    now = datetime.now(UTC).isoformat()
    result = {"checked": 0, "updated": 0, "removed": 0, "failed": 0, "skipped": 0}
    show_seasons_to_refresh: set[tuple[int, int]] = set()
    log_info(logger, "sanitizer.started", "Sanitizer cycle started")

    health = client.health()
    if not health.ok:
        message = f"AnimeWorld unreachable: {health.url}"
        log_warning(logger, "sanitizer.skipped", "Sanitizer skipped", lines=[message])
        _set_state(last_result=result, last_error=message, last_finished_at=now, running=False)
        return result

    with get_db() as conn:
        show_rows = [dict(row) for row in conn.execute(
            """
            SELECT asm.id, asm.show_id, asm.season_number, asm.aw_link, asm.aw_title, asm.aw_status, asm.aw_category, asm.confidence_factors, asm.link_check_failures
            FROM aw_show_mappings asm
            JOIN show_seasons ss
              ON ss.show_id = asm.show_id
             AND ss.season_number = asm.season_number
            WHERE COALESCE(ss.ignored, 0) = 0
              AND COALESCE(asm.mapping_type, 'auto') = 'auto'
            ORDER BY asm.id
            """
        ).fetchall()]
        movie_rows = [dict(row) for row in conn.execute(
            """
            SELECT amm.id, amm.movie_id, amm.aw_link, amm.aw_title, amm.aw_status, amm.aw_category, amm.confidence_factors, amm.link_check_failures
            FROM aw_movie_mappings amm
            JOIN movies m ON m.id = amm.movie_id
            WHERE COALESCE(m.ignored, 0) = 0
              AND COALESCE(amm.mapping_type, 'auto') = 'auto'
            ORDER BY amm.id
            """
        ).fetchall()]
        for row in conn.execute(
            """
            SELECT DISTINCT asm.show_id, asm.season_number
            FROM aw_show_mappings asm
            JOIN show_seasons ss
              ON ss.show_id = asm.show_id
             AND ss.season_number = asm.season_number
            WHERE COALESCE(asm.mapping_type, 'auto') = 'auto'
              AND COALESCE(ss.ignored, 0) = 0
            ORDER BY asm.show_id, asm.season_number
            """
        ).fetchall():
            show_seasons_to_refresh.add((int(row["show_id"]), int(row["season_number"])))

    pending_show_retries = list_show_sanitizer_retries()
    pending_movie_retries = list_movie_sanitizer_retries()
    show_ids = {int(row["show_id"]) for row in show_rows}
    show_ids.update(show_id for show_id, _ in show_seasons_to_refresh)
    show_ids.update(int(row["show_id"]) for row in pending_show_retries)
    movie_ids = {int(row["movie_id"]) for row in movie_rows}
    movie_ids.update(int(row["movie_id"]) for row in pending_movie_retries)
    show_details = {show_id: get_show_detail(show_id) for show_id in sorted(show_ids)}
    movie_details = {movie_id: get_movie_detail(movie_id) for movie_id in sorted(movie_ids)}

    slug_results: dict[str, dict | Exception] = {}
    all_slugs = sorted({str(row.get("aw_link") or "").strip() for row in show_rows + movie_rows if str(row.get("aw_link") or "").strip()})
    if all_slugs:
        with ThreadPoolExecutor(max_workers=min(_NETWORK_WORKERS, len(all_slugs))) as executor:
            future_map = {executor.submit(_fetch_slug_verification, slug): slug for slug in all_slugs}
            for future in as_completed(future_map):
                slug = future_map[future]
                try:
                    slug_results[slug] = future.result()
                except Exception as exc:
                    slug_results[slug] = exc

    metadata_needed_slugs: set[str] = set()
    for row in show_rows:
        verification = slug_results.get(str(row.get("aw_link") or "").strip())
        if not isinstance(verification, dict):
            continue
        final_slug = str(verification.get("final_slug") or "").strip()
        if final_slug and (final_slug != str(row.get("aw_link") or "").strip() or _row_is_preaired_placeholder(row)):
            metadata_needed_slugs.add(final_slug)
    for row in movie_rows:
        verification = slug_results.get(str(row.get("aw_link") or "").strip())
        if not isinstance(verification, dict):
            continue
        final_slug = str(verification.get("final_slug") or "").strip()
        if final_slug and final_slug != str(row.get("aw_link") or "").strip():
            metadata_needed_slugs.add(final_slug)

    final_slugs = sorted(metadata_needed_slugs)
    slug_metadata: dict[str, dict | Exception] = {}
    if final_slugs:
        with ThreadPoolExecutor(max_workers=min(_NETWORK_WORKERS, len(final_slugs))) as executor:
            future_map = {executor.submit(_fetch_slug_metadata, slug): slug for slug in final_slugs}
            for future in as_completed(future_map):
                slug = future_map[future]
                try:
                    slug_metadata[slug] = future.result()
                except Exception as exc:
                    slug_metadata[slug] = exc

    for row in show_rows:
        result["checked"] += 1
        aw_link = str(row.get("aw_link") or "").strip()
        verification = slug_results.get(aw_link)
        try:
            if isinstance(verification, Exception):
                raise verification
            if not isinstance(verification, dict):
                raise RuntimeError(f"Missing sanitizer verification result for {aw_link}")
            if bool(verification.get("is_soft_404")):
                raise _Soft404MappingError(aw_link)
            new_slug = str(verification.get("final_slug") or aw_link).strip()
            needs_refresh = new_slug != str(row.get("aw_link") or "").strip() or _row_is_preaired_placeholder(row)
            if needs_refresh:
                metadata = slug_metadata.get(new_slug)
                if isinstance(metadata, Exception):
                    raise metadata
                if not metadata:
                    raise RuntimeError(f"Missing sanitizer metadata for {new_slug}")
                show = show_details.get(int(row["show_id"]))
                season = _season_by_number(show or {}, int(row["season_number"]))
                if not show or not season:
                    continue
                if _refresh_show_mapping_metadata(show, season, row, metadata, now):
                    result["updated"] += 1
            else:
                _touch_show_mapping(int(row["id"]), now)
        except Exception as exc:
            soft404 = isinstance(exc, _Soft404MappingError)
            if _is_transient_aw_error(exc):
                result["skipped"] += 1
                log_warning(
                    logger,
                    "sanitizer.show.skipped",
                    "Show mapping verification skipped",
                    lines=[f"{display_aw_link(row['aw_link'])} (transient AnimeWorld error)"],
                    entity_kind="show",
                    entity_id=row.get("show_id"),
                )
                continue
            failures = int(row["link_check_failures"] or 0) + 1
            reason_suffix = " [not-found page]" if soft404 else ""
            if failures >= 2:
                with get_db(write=True) as conn:
                    conn.execute("DELETE FROM aw_show_mappings WHERE id = ?", (row["id"],))
                queue_show_sanitizer_retry(int(row["show_id"]), int(row["season_number"]))
                _publish_library_change("show", int(row["show_id"]))
                log_warning(
                    logger,
                    "sanitizer.show.removed",
                    "Removed dead show mapping",
                    lines=[f"{display_aw_link(row['aw_link'])}{reason_suffix}"],
                    entity_kind="show",
                    entity_id=row.get("show_id"),
                )
                result["removed"] += 1
            else:
                with get_db(write=True) as conn:
                    conn.execute(
                        "UPDATE aw_show_mappings SET link_check_failures = ?, updated_at = ? WHERE id = ?",
                        (failures, now, row["id"]),
                    )
                log_warning(
                    logger,
                    "sanitizer.show.failed",
                    "Show mapping verification failed",
                    lines=[f"{display_aw_link(row['aw_link'])} ({failures}/2){reason_suffix}"],
                    entity_kind="show",
                    entity_id=row.get("show_id"),
                )
                result["failed"] += 1

    for row in movie_rows:
        result["checked"] += 1
        aw_link = str(row.get("aw_link") or "").strip()
        verification = slug_results.get(aw_link)
        try:
            if isinstance(verification, Exception):
                raise verification
            if not isinstance(verification, dict):
                raise RuntimeError(f"Missing sanitizer verification result for {aw_link}")
            if bool(verification.get("is_soft_404")):
                raise _Soft404MappingError(aw_link)
            new_slug = str(verification.get("final_slug") or aw_link).strip()
            needs_refresh = new_slug != str(row.get("aw_link") or "").strip()
            if needs_refresh:
                metadata = slug_metadata.get(new_slug)
                if isinstance(metadata, Exception):
                    raise metadata
                if not metadata:
                    raise RuntimeError(f"Missing sanitizer metadata for {new_slug}")
                movie = movie_details.get(int(row["movie_id"]))
                if not movie:
                    continue
                if _refresh_movie_mapping_metadata(movie, row, metadata, now):
                    result["updated"] += 1
            else:
                _touch_movie_mapping(int(row["id"]), now)
        except Exception as exc:
            soft404 = isinstance(exc, _Soft404MappingError)
            if _is_transient_aw_error(exc):
                result["skipped"] += 1
                log_warning(
                    logger,
                    "sanitizer.movie.skipped",
                    "Movie mapping verification skipped",
                    lines=[f"{display_aw_link(row['aw_link'])} (transient AnimeWorld error)"],
                    entity_kind="movie",
                    entity_id=row.get("movie_id"),
                )
                continue
            failures = int(row["link_check_failures"] or 0) + 1
            reason_suffix = " [not-found page]" if soft404 else ""
            if failures >= 2:
                with get_db(write=True) as conn:
                    conn.execute("DELETE FROM aw_movie_mappings WHERE id = ?", (row["id"],))
                queue_movie_sanitizer_retry(int(row["movie_id"]))
                _publish_library_change("movie", int(row["movie_id"]))
                log_warning(
                    logger,
                    "sanitizer.movie.removed",
                    "Removed dead movie mapping",
                    lines=[f"{display_aw_link(row['aw_link'])}{reason_suffix}"],
                    entity_kind="movie",
                    entity_id=row.get("movie_id"),
                )
                result["removed"] += 1
            else:
                with get_db(write=True) as conn:
                    conn.execute(
                        "UPDATE aw_movie_mappings SET link_check_failures = ?, updated_at = ? WHERE id = ?",
                        (failures, now, row["id"]),
                    )
                log_warning(
                    logger,
                    "sanitizer.movie.failed",
                    "Movie mapping verification failed",
                    lines=[f"{display_aw_link(row['aw_link'])} ({failures}/2){reason_suffix}"],
                    entity_kind="movie",
                    entity_id=row.get("movie_id"),
                )
                result["failed"] += 1

    from .automap_service import automap_movie, automap_show

    for retry in list_show_sanitizer_retries():
        show_id = int(retry["show_id"])
        season_number = int(retry["season_number"])
        show = get_show_detail(show_id)
        if not show:
            clear_show_sanitizer_retry(show_id, season_number)
            continue
        season = _season_by_number(show, season_number)
        if not season:
            clear_show_sanitizer_retry(show_id, season_number)
            continue
        if bool(season.get("ignored")):
            clear_show_sanitizer_retry(show_id, season_number)
            continue
        mappings = list(season.get("mappings") or [])
        if mappings:
            clear_show_sanitizer_retry(show_id, season_number)
            continue
        response = automap_show(show_id, season_number=season_number, force=False)
        if response.get("status") in {"success", "partial"} and season_number in set(response.get("mapped_seasons") or []):
            clear_show_sanitizer_retry(show_id, season_number)
            result["updated"] += 1

    for retry in list_movie_sanitizer_retries():
        movie_id = int(retry["movie_id"])
        movie = get_movie_detail(movie_id)
        if not movie:
            clear_movie_sanitizer_retry(movie_id)
            continue
        if bool(movie.get("ignored")):
            clear_movie_sanitizer_retry(movie_id)
            continue
        if movie.get("mapping"):
            clear_movie_sanitizer_retry(movie_id)
            continue
        response = automap_movie(movie_id, force=False)
        if response.get("status") == "success":
            clear_movie_sanitizer_retry(movie_id)
            result["updated"] += 1

    for show_id, season_number in sorted(show_seasons_to_refresh):
        show = get_show_detail(show_id)
        if not show:
            continue
        season = _season_by_number(show, season_number)
        if not season or not _needs_show_mapping_refresh(show, season):
            continue
        from .automap_service import automap_show

        response = automap_show(show_id, season_number=season_number, force=True)
        if response.get("status") in {"success", "partial"}:
            result["updated"] += 1
            log_info(
                logger,
                "sanitizer.show.refreshed",
                "Refreshed stale auto mapping",
                lines=[f"show_id={show_id} season={season_number} status={response.get('status')}"],
                entity_kind="show",
                entity_id=show_id,
            )

    _set_state(last_result=result, last_error="", last_finished_at=now, running=False)
    log_block(
        logger,
        logging.INFO,
        "Sanitizer cycle complete",
        [
            f"checked={result['checked']}",
            f"updated={result['updated']}",
            f"removed={result['removed']}",
            f"failed={result['failed']}",
            f"skipped={result['skipped']}",
        ],
        event_type="sanitizer.finished",
        details=dict(result),
    )
    return result


def start_link_sanitizer() -> dict:
    if not settings.sanitizer_enabled:
        _set_state(enabled=False, running=False, last_error="")
        log_info(logger, "sanitizer.request.ignored", "Sanitizer request ignored", lines=["disabled by env"])
        return {"ok": False, "disabled": True, "message": "Sanitizer disabled by env"}

    def worker():
        _set_state(enabled=True, running=True, last_started_at=datetime.now(UTC).isoformat(), last_error="")
        try:
            sanitize_links_once()
        except Exception as exc:
            _set_state(last_error=str(exc), last_finished_at=datetime.now(UTC).isoformat(), running=False)
            raise

    threading.Thread(target=worker, name="awc-link-sanitizer", daemon=True).start()
    log_info(logger, "sanitizer.requested", "Sanitizer run requested")
    return {"ok": True, "message": "Mapped links sanitizer started"}
