"""Automatic mapping workflows for shows and movies."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import threading

from ..core.config import settings
from ..core.log_events import format_movie_automap_lines, format_show_automap_lines, log_block, log_info, log_warning
from ..core.logging import get_logger
from ..domain.release_window import has_started
from ..repositories.db import get_db
from ..repositories.mappings import (
    list_show_mappings,
    remove_movie_mapping,
    replace_movie_mapping_auto,
    replace_show_mappings_auto,
)
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .automap_candidates import discover_candidates_for_titles
from .automap_language import resolve_movie_language_preference, resolve_show_language_preference
from .automap_scoring import calculate_movie_confidence, calculate_show_confidence
from .events_service import publish_library_batch, publish_library_card_changed, publish_library_stats_changed

_automap_lock = threading.Lock()
logger = get_logger("automap")
_automap_state = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": "",
    "last_result": None,
}


def automap_status() -> dict:
    with _automap_lock:
        return dict(_automap_state)


def _set_state(**updates) -> None:
    with _automap_lock:
        _automap_state.update(updates)


def _publish_library_change(kind: str, item_id: int) -> None:
    publish_library_card_changed(kind, item_id)
    publish_library_stats_changed()


def _summarize_automap_all(result: dict) -> dict:
    show_results = list(result.get("shows") or [])
    movie_results = list(result.get("movies") or [])
    return {
        "shows": len(show_results),
        "movies": len(movie_results),
        "mapped_show_seasons": sum(len(item.get("mapped_seasons") or []) for item in show_results),
        "mapped_movies": sum(1 for item in movie_results if item.get("status") == "success"),
        "ambiguous_shows": sum(1 for item in show_results if item.get("status") in {"ambiguous", "partial"}),
        "not_found_shows": sum(1 for item in show_results if item.get("status") == "not_found"),
        "not_found_movies": sum(1 for item in movie_results if item.get("status") == "not_found"),
    }


def _show_alternate_titles(show: dict) -> list[str]:
    return [item.get("title", "") for item in show.get("alternate_titles", []) if item.get("title")]


def _discovery_candidates(title: str, alternates: list[str], limit: int | None = None) -> list[dict]:
    alt_payload = [{"title": value} for value in alternates if value]
    return discover_candidates_for_titles(title, alt_payload, limit=limit)


def _existing_links_by_show(show: dict) -> set[str]:
    links: set[str] = set()
    for season in show.get("seasons", []):
        for mapping in season.get("mappings", []):
            link = (mapping.get("aw_link") or "").strip()
            if not link:
                continue
            for part in link.split(","):
                part = part.strip()
                if part:
                    links.add(part)
    return links


def _filter_language_candidates(candidates: list[dict], want_dubbed: bool) -> list[dict]:
    if not candidates:
        return candidates

    def is_italian(candidate: dict) -> bool:
        return "ital" in str(candidate.get("aw_audio") or "").lower()

    def is_japanese(candidate: dict) -> bool:
        audio = str(candidate.get("aw_audio") or "").lower()
        return "giapp" in audio or "japan" in audio

    if want_dubbed:
        dubbed = [candidate for candidate in candidates if bool(candidate.get("dub"))]
        if dubbed:
            return dubbed
        italian = [candidate for candidate in candidates if is_italian(candidate)]
        if italian:
            return italian
        return candidates

    non_dubbed = [candidate for candidate in candidates if not bool(candidate.get("dub"))]
    japanese = [candidate for candidate in non_dubbed if is_japanese(candidate)]
    if japanese:
        return japanese
    if non_dubbed:
        return non_dubbed
    return candidates


def _build_scored_candidates(show: dict, season: dict, candidates: list[dict], want_dubbed: bool, reserved_links: set[str]) -> list[dict]:
    own_links = {
        (mapping.get("aw_link") or "").strip()
        for mapping in season.get("mappings", [])
        if mapping.get("aw_link")
    }
    blocked_links = reserved_links - own_links
    filtered = _filter_language_candidates(candidates, want_dubbed)
    scored: list[dict] = []
    for candidate in filtered:
        if candidate.get("aw_link", "") in blocked_links:
            continue
        score, factors = calculate_show_confidence(show, season, candidate, want_dubbed=want_dubbed)
        scored.append({**candidate, "confidence_score": score, "confidence_factors": factors})
    scored.sort(key=lambda item: item["confidence_score"], reverse=True)
    return scored


def _season_segments(season: dict) -> list[dict]:
    markers = season.get("segment_markers") or []
    valid = [dict(item) for item in markers if int(item.get("count") or 0) > 0]
    return valid if len(valid) > 1 else []


def _segment_marker_bonus(parts: list[dict], markers: list[dict]) -> tuple[float, dict]:
    exact_count_hits = 0
    date_hits = 0
    for part, marker in zip(parts, markers):
        if abs(int(part.get("aw_episode_count") or 0) - int(marker.get("count") or 0)) <= 1:
            exact_count_hits += 1
        release = part.get("aw_release_datetime")
        marker_start = str(marker.get("air_date_start") or "")
        if release and marker_start:
            try:
                marker_dt = datetime.fromisoformat(marker_start.replace("Z", "+00:00"))
                if abs((release.date() - marker_dt.date()).days) <= 14:
                    date_hits += 1
            except (ValueError, AttributeError):
                pass
    bonus = min(exact_count_hits * 0.02 + date_hits * 0.02, 0.10)
    return bonus, {
        "segment_exact_matches": exact_count_hits,
        "segment_date_matches": date_hits,
        "segment_bonus": round(bonus, 3),
    }


def _detect_segment_chain(show: dict, season: dict, season_scores: list[dict], want_dubbed: bool) -> tuple[list[dict], float, dict] | None:
    target_count = int(season.get("episode_count") or 0)
    if not target_count:
        return None

    markers = _season_segments(season)
    if not markers:
        return None

    season_end = season.get("air_date_end") or ""
    required_counts = [int(item.get("count") or 0) for item in markers]
    best_chain: tuple[list[dict], float, dict] | None = None

    candidates = [
        candidate for candidate in season_scores
        if int(candidate.get("aw_episode_count") or 0) > 0
    ]

    def fits_marker(candidate: dict, marker: dict, prev_release: datetime | None) -> bool:
        count = int(candidate.get("aw_episode_count") or 0)
        target = int(marker.get("count") or 0)
        if abs(count - target) > 1:
            return False
        if (candidate.get("aw_status") or "").lower() != "finito":
            return False
        release = candidate.get("aw_release_datetime")
        if prev_release and release and release <= prev_release:
            return False
        if season_end and release:
            try:
                last_aired_dt = datetime.fromisoformat(str(season_end).replace("Z", "+00:00"))
                if release.date() > last_aired_dt.date() and (release.date() - last_aired_dt.date()).days > 30:
                    return False
            except (ValueError, AttributeError):
                pass
        marker_start = str(marker.get("air_date_start") or "")
        if release and marker_start:
            try:
                marker_dt = datetime.fromisoformat(marker_start.replace("Z", "+00:00"))
                if abs((release.date() - marker_dt.date()).days) > 45:
                    return False
            except (ValueError, AttributeError):
                pass
        return True

    def backtrack(index: int, chain: list[dict]) -> None:
        nonlocal best_chain
        if index >= len(required_counts):
            combined = sum(int(item.get("aw_episode_count") or 0) for item in chain)
            if abs(combined - target_count) > 1:
                return
            first = chain[0]
            combined_candidate = {
                **first,
                "aw_title": " + ".join(item.get("aw_title", "") for item in chain),
                "aw_episode_count": combined,
                "aw_total_episodes": sum(int(item.get("aw_total_episodes") or item.get("aw_episode_count") or 0) for item in chain),
                "aw_release_datetime": first.get("aw_release_datetime"),
            }
            combined_score, combined_factors = calculate_show_confidence(show, season, combined_candidate, want_dubbed=want_dubbed)
            bonus, bonus_factors = _segment_marker_bonus(chain, markers)
            combined_score += bonus
            combined_factors = {**combined_factors, **bonus_factors, "split_cour": True}
            if combined_score < settings.automap_confidence_threshold:
                return
            if best_chain is None or combined_score > best_chain[1]:
                best_chain = (list(chain), combined_score, combined_factors)
            return

        prev_release = chain[-1].get("aw_release_datetime") if chain else None
        prev_audio = (chain[-1].get("aw_audio") or "").lower() if chain else ""
        for candidate in candidates:
            if any(candidate["aw_link"] == item["aw_link"] for item in chain):
                continue
            if prev_audio and (candidate.get("aw_audio") or "").lower() != prev_audio:
                continue
            if not fits_marker(candidate, markers[index], prev_release):
                continue
            chain.append(candidate)
            backtrack(index + 1, chain)
            chain.pop()

    backtrack(0, [])
    return best_chain


def _propagate_single_link(
    *,
    show_id: int,
    eligible_seasons: list[dict],
    start_index: int,
    best: dict,
    mapped_seasons: list[int],
    handled: set[int],
    reserved_links: set[str],
) -> None:
    available = int(best.get("aw_episode_count") or 0)
    is_ongoing = (best.get("aw_status") or "").lower() != "finito"
    if available <= 0:
        return

    chain: list[dict] = []
    consumed = 0
    for season in eligible_seasons[start_index:]:
        sn = season["season_number"]
        if sn in handled:
            continue
        if not bool(season.get("has_aired", True)):
            break
        season_count = int(season.get("episode_count") or 0)
        if season_count <= 0:
            break
        # For ongoing AW entries the episode count will keep growing, so the
        # current count may lag behind Sonarr's arc breakdown.  Only apply
        # the episode-count cap for finished shows.
        if not is_ongoing and consumed + season_count > available:
            break
        consumed += season_count
        chain.append(season)

    if not chain:
        return

    root_sn = chain[0]["season_number"]
    for index, season in enumerate(chain):
        sn = season["season_number"]
        factors = dict(best["confidence_factors"])
        if index > 0:
            factors["linked"] = root_sn
        replace_show_mappings_auto(
            show_id=show_id,
            season_number=sn,
            items=[
                {
                    "part": 1,
                    "aw_link": best["aw_link"],
                    "aw_title": best["aw_title"],
                    "aw_episode_count": best["aw_episode_count"],
                    "aw_total_episodes": best["aw_total_episodes"],
                    "aw_status": best.get("aw_status", ""),
                    "aw_category": best.get("aw_category", ""),
                    "confidence_score": best["confidence_score"],
                    "confidence_factors": json.dumps(factors),
                    "linked_with_season": None if index == 0 else root_sn,
                }
            ],
        )
        mapped_seasons.append(sn)
        handled.add(sn)

    reserved_links.add(best["aw_link"])


def automap_movie(movie_id: int, force: bool = False, *, emit_logs: bool = True) -> dict:
    movie = get_movie_detail(movie_id)
    if not movie:
        log_warning(logger, "automap.movie.not_found", "Movie automap failed: movie not found", details={"movie_id": movie_id}, entity_kind="movie", entity_id=movie_id)
        return {"status": "error", "message": "movie_not_found", "movie_id": movie_id}
    if bool(movie.get("ignored")):
        if emit_logs:
            log_info(logger, "automap.movie.ignored", "Movie automap skipped: ignored", entity_kind="movie", entity_id=movie_id, entity_title=movie.get("title"))
        return {"status": "already_mapped", "movie_id": movie_id, "ignored": True}
    if movie.get("mapping") and not force:
        if emit_logs:
            log_info(logger, "automap.movie.already_mapped", "Movie automap skipped: already mapped", entity_kind="movie", entity_id=movie_id, entity_title=movie.get("title"))
        return {"status": "already_mapped", "movie_id": movie_id}

    want_dubbed = resolve_movie_language_preference(movie)
    candidates = []
    for candidate in _filter_language_candidates(_discovery_candidates(
        movie["title"],
        [item.get("title", "") for item in movie.get("alternate_titles", [])],
    ), want_dubbed):
        score, factors = calculate_movie_confidence(movie, candidate, want_dubbed=want_dubbed)
        candidates.append({**candidate, "confidence_score": score, "confidence_factors": factors})
    candidates.sort(key=lambda item: item["confidence_score"], reverse=True)

    if not candidates or candidates[0]["confidence_score"] < settings.automap_movie_confidence_threshold:
        if force:
            removed = remove_movie_mapping(movie_id)
            if removed:
                _publish_library_change("movie", movie_id)
        if emit_logs:
            log_block(
                logger,
                logging.WARNING,
                movie.get("title", f"movie:{movie_id}"),
                ["No high-confidence movie match found"],
                event_type="automap.movie.not_found",
                entity_kind="movie",
                entity_id=movie_id,
                entity_title=movie.get("title"),
            )
        return {"status": "not_found", "movie_id": movie_id, "candidates": candidates[:5]}

    best = candidates[0]
    mapping = replace_movie_mapping_auto(
        movie_id=movie_id,
        aw_link=best["aw_link"],
        aw_title=best["aw_title"],
        aw_status=best.get("aw_status", ""),
        aw_category=best.get("aw_category", ""),
        confidence_score=best["confidence_score"],
        confidence_factors=json.dumps(best["confidence_factors"]),
    )
    _publish_library_change("movie", movie_id)
    if emit_logs:
        log_block(
            logger,
            logging.INFO,
            movie.get("title", f"movie:{movie_id}"),
            format_movie_automap_lines(mapping),
            event_type="automap.movie.mapped",
            entity_kind="movie",
            entity_id=movie_id,
            entity_title=movie.get("title"),
        )
    return {"status": "success", "movie_id": movie_id, "mapping": mapping, "candidates": candidates[:5]}


def automap_show(show_id: int, season_number: int | None = None, force: bool = False, *, emit_logs: bool = True) -> dict:
    show = get_show_detail(show_id)
    if not show:
        log_warning(logger, "automap.show.not_found", "Show automap failed: show not found", details={"show_id": show_id}, entity_kind="show", entity_id=show_id)
        return {"status": "error", "message": "show_not_found", "show_id": show_id}

    want_dubbed = resolve_show_language_preference(show)
    scored_candidates: list[dict] = []
    mapped_seasons: list[int] = []
    ambiguous: list[dict] = []
    handled: set[int] = set()
    skipped_unaired: list[int] = []
    ignored_seasons: list[int] = []

    target_seasons = []
    for season in show.get("seasons", []):
        season_number_value = int(season.get("season_number", 0) or 0)
        if season_number_value <= 0:
            continue
        if season_number is not None and season_number_value != season_number:
            continue
        if bool(season.get("ignored")):
            ignored_seasons.append(season_number_value)
            continue
        target_seasons.append({**season, "has_aired": has_started(season.get("air_date_start"))})
    if not target_seasons:
        if ignored_seasons:
            if emit_logs:
                log_info(logger, "automap.show.ignored", "Show automap skipped: ignored", entity_kind="show", entity_id=show_id, entity_title=show.get("title"))
            return {"status": "already_mapped", "show_id": show_id, "mapped_seasons": [], "ambiguous": [], "candidates": [], "ignored_seasons": sorted(ignored_seasons)}
        if emit_logs:
            log_warning(logger, "automap.show.no_target_seasons", "Show automap found no target seasons", entity_kind="show", entity_id=show_id, entity_title=show.get("title"))
        return {"status": "not_found", "show_id": show_id, "mapped_seasons": [], "ambiguous": [], "candidates": [], "ignored_seasons": []}

    eligible_seasons = [
        season for season in target_seasons
        if force or not list_show_mappings(show_id, season["season_number"])
    ]
    if not eligible_seasons:
        if emit_logs:
            log_info(logger, "automap.show.already_mapped", "Show automap skipped: already mapped", entity_kind="show", entity_id=show_id, entity_title=show.get("title"))
        return {"status": "already_mapped", "show_id": show_id, "mapped_seasons": [], "ambiguous": [], "candidates": [], "ignored_seasons": sorted(ignored_seasons)}

    candidates = _discovery_candidates(show["title"], _show_alternate_titles(show))

    reserved_links = _existing_links_by_show(show)

    for index, season in enumerate(eligible_seasons):
        sn = season["season_number"]
        if sn in handled:
            continue

        season_scores = _build_scored_candidates(show, season, candidates, want_dubbed, reserved_links)
        if season_scores:
            scored_candidates.extend(
                [{**candidate, "season_number": sn} for candidate in season_scores[:3]]
            )

        target_count = int(season.get("episode_count") or 0)
        best = season_scores[0] if season_scores else None
        second = season_scores[1] if len(season_scores) > 1 else None
        season_has_aired = bool(season.get("has_aired", True))

        split_pair = _detect_segment_chain(show, season, season_scores, want_dubbed)
        if split_pair:
            parts, split_score, split_factors = split_pair
            replace_show_mappings_auto(
                show_id=show_id,
                season_number=sn,
                items=[
                    {
                        "part": part_index,
                        "aw_link": candidate["aw_link"],
                        "aw_title": candidate["aw_title"],
                        "aw_episode_count": candidate["aw_episode_count"],
                        "aw_total_episodes": candidate["aw_total_episodes"],
                        "aw_status": candidate.get("aw_status", ""),
                        "aw_category": candidate.get("aw_category", ""),
                        "confidence_score": split_score,
                        "confidence_factors": json.dumps({**split_factors, "split_cour": True}),
                        "linked_with_season": None,
                    }
                    for part_index, candidate in enumerate(parts, start=1)
                ],
            )
            mapped_seasons.append(sn)
            handled.add(sn)
            for candidate in parts:
                reserved_links.add(candidate["aw_link"])
            continue

        if not season_has_aired:
            preaired = [
                candidate for candidate in season_scores
                if candidate.get("aw_is_placeholder") and candidate["confidence_score"] >= settings.automap_confidence_threshold
            ]
            if preaired:
                best_preaired = preaired[0]
                replace_show_mappings_auto(
                    show_id=show_id,
                    season_number=sn,
                    items=[
                        {
                            "part": 1,
                            "aw_link": best_preaired["aw_link"],
                            "aw_title": best_preaired["aw_title"],
                            "aw_episode_count": best_preaired["aw_episode_count"],
                            "aw_total_episodes": best_preaired["aw_total_episodes"],
                            "aw_status": best_preaired.get("aw_status", ""),
                            "aw_category": best_preaired.get("aw_category", ""),
                            "confidence_score": best_preaired["confidence_score"],
                            "confidence_factors": json.dumps({**best_preaired["confidence_factors"], "preaired": True}),
                            "linked_with_season": None,
                        }
                    ],
                )
                mapped_seasons.append(sn)
                handled.add(sn)
                reserved_links.add(best_preaired["aw_link"])
                continue
            # Unaired seasons should never degrade into ambiguous "needs review".
            # We either promote a verified placeholder match, or we skip them silently
            # until AnimeWorld exposes a real placeholder/released page worth mapping.
            skipped_unaired.append(sn)
            continue

        if best and best["confidence_score"] >= settings.automap_confidence_threshold and (
            not second or (best["confidence_score"] - second["confidence_score"]) >= 0.05
        ):
            if int(best.get("aw_episode_count") or 0) >= target_count > 0:
                _propagate_single_link(
                    show_id=show_id,
                    eligible_seasons=eligible_seasons,
                    start_index=index,
                    best=best,
                    mapped_seasons=mapped_seasons,
                    handled=handled,
                    reserved_links=reserved_links,
                )
                if sn in handled:
                    continue

            replace_show_mappings_auto(
                show_id=show_id,
                season_number=sn,
                items=[
                    {
                        "part": 1,
                        "aw_link": best["aw_link"],
                        "aw_title": best["aw_title"],
                        "aw_episode_count": best["aw_episode_count"],
                        "aw_total_episodes": best["aw_total_episodes"],
                        "aw_status": best.get("aw_status", ""),
                        "aw_category": best.get("aw_category", ""),
                        "confidence_score": best["confidence_score"],
                        "confidence_factors": json.dumps(best["confidence_factors"]),
                        "linked_with_season": None,
                    }
                ],
            )
            mapped_seasons.append(sn)
            handled.add(sn)
            reserved_links.add(best["aw_link"])
            continue

        ambiguous.append({"season": sn, "candidates": season_scores[:5]})

    status = "success" if mapped_seasons else "not_found"
    if ambiguous and mapped_seasons:
        status = "partial"
    elif ambiguous and not mapped_seasons:
        status = "ambiguous"
    elif skipped_unaired and not mapped_seasons:
        status = "already_mapped" if any(list_show_mappings(show_id, season["season_number"]) for season in target_seasons) else "not_found"

    refreshed_show = get_show_detail(show_id) if mapped_seasons else show
    if mapped_seasons:
        _publish_library_change("show", show_id)
    if emit_logs:
        if status == "success":
            log_block(
                logger,
                logging.INFO,
                show.get("title", f"show:{show_id}"),
                format_show_automap_lines(refreshed_show or show, mapped_seasons, []),
                event_type="automap.show.success",
                entity_kind="show",
                entity_id=show_id,
                entity_title=show.get("title"),
            )
        elif status == "partial":
            log_block(
                logger,
                logging.WARNING,
                show.get("title", f"show:{show_id}"),
                format_show_automap_lines(refreshed_show or show, mapped_seasons, ambiguous),
                event_type="automap.show.partial",
                entity_kind="show",
                entity_id=show_id,
                entity_title=show.get("title"),
            )
        elif status == "ambiguous":
            log_block(
                logger,
                logging.WARNING,
                show.get("title", f"show:{show_id}"),
                format_show_automap_lines(show, [], ambiguous),
                event_type="automap.show.ambiguous",
                entity_kind="show",
                entity_id=show_id,
                entity_title=show.get("title"),
            )
        elif status == "not_found":
            log_block(
                logger,
                logging.WARNING,
                show.get("title", f"show:{show_id}"),
                ["No high-confidence AnimeWorld match found"],
                event_type="automap.show.not_found",
                entity_kind="show",
                entity_id=show_id,
                entity_title=show.get("title"),
            )
    return {
        "status": status,
        "show_id": show_id,
        "mapped_seasons": sorted(mapped_seasons),
        "ambiguous": ambiguous,
        "candidates": scored_candidates[:10],
        "skipped_unaired": sorted(skipped_unaired),
        "ignored_seasons": sorted(ignored_seasons),
    }


def _run_background(target, *args, **kwargs) -> dict:
    with _automap_lock:
        if _automap_state.get("running"):
            return {"status": "already_running"}
        _automap_state.update(
            running=True,
            last_started_at=datetime.now(UTC).isoformat(),
            last_error="",
        )

    def worker():
        try:
            result = target(*args, **kwargs)
            _set_state(last_result=result)
            if target is automap_all:
                summary = _summarize_automap_all(result)
                log_info(
                    logger,
                    "automap.library.finished",
                    "Library automap completed",
                    lines=[
                        f"shows={summary['shows']}",
                        f"movies={summary['movies']}",
                        f"mapped_show_seasons={summary['mapped_show_seasons']}",
                        f"mapped_movies={summary['mapped_movies']}",
                    ],
                    details=summary,
                )
                publish_library_batch("automap", "finished", **summary)
        except Exception as exc:
            _set_state(last_error=str(exc))
            raise
        finally:
            _set_state(running=False, last_finished_at=datetime.now(UTC).isoformat())

    thread = threading.Thread(target=worker, name="awc-automap", daemon=True)
    thread.start()
    return {"status": "started"}


def automap_all(force: bool = False) -> dict:
    with get_db() as conn:
        if force:
            show_rows = conn.execute(
                """
                SELECT DISTINCT s.id, s.title
                FROM shows s
                JOIN show_seasons ss ON ss.show_id = s.id
                WHERE ss.season_number > 0
                  AND COALESCE(ss.ignored, 0) = 0
                """
            ).fetchall()
            movie_rows = conn.execute(
                """
                SELECT m.id, m.title
                FROM movies m
                WHERE COALESCE(m.ignored, 0) = 0
                """
            ).fetchall()
        else:
            show_rows = conn.execute(
                """
                SELECT DISTINCT s.id, s.title
                FROM shows s
                JOIN show_seasons ss ON ss.show_id = s.id
                LEFT JOIN aw_show_mappings asm
                    ON asm.show_id = ss.show_id
                   AND asm.season_number = ss.season_number
                WHERE ss.season_number > 0
                  AND COALESCE(ss.ignored, 0) = 0
                  AND asm.id IS NULL
                """
            ).fetchall()
            movie_rows = conn.execute(
                """
                SELECT m.id, m.title
                FROM movies m
                LEFT JOIN aw_movie_mappings amm ON amm.movie_id = m.id
                WHERE COALESCE(m.ignored, 0) = 0
                  AND amm.id IS NULL
                """
            ).fetchall()
        shows = [("show", row[0], row[1]) for row in show_rows]
        movies = [("movie", row[0], row[1]) for row in movie_rows]
    combined = sorted(shows + movies, key=lambda item: (item[2] or "").lower())
    show_results = []
    movie_results = []
    for kind, item_id, _ in combined:
        if kind == "show":
            show_results.append(automap_show(item_id, force=force))
        else:
            movie_results.append(automap_movie(item_id, force=force))
    return {"shows": show_results, "movies": movie_results}


def start_automap_all(force: bool = False) -> dict:
    log_info(
        logger,
        "automap.library.started",
        "Library automap started",
        lines=[f"force={'true' if force else 'false'}"],
        details={"force": force},
    )
    publish_library_batch("automap", "started", force=force)
    return _run_background(automap_all, force)
