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
from ..repositories.shows import get_show_detail
from ..repositories.mappings import (
    list_show_mappings,
    remove_movie_mapping,
    replace_movie_mapping_auto,
    replace_show_mappings_auto,
)
from ..repositories.movies import get_movie_detail
from .automap_candidates import discover_candidates_for_titles
from .automap_language import resolve_movie_language_preference, resolve_show_language_preference
from .automap_scoring import calculate_movie_confidence, calculate_show_confidence
from .events_service import publish_library_batch, publish_library_card_changed, publish_library_stats_changed

_automap_lock = threading.Lock()
logger = get_logger("automap")
_automap_state = {
    "running": False,
    "cancel_requested": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_stop_requested_at": None,
    "last_error": "",
    "last_result": None,
}


def automap_status() -> dict:
    with _automap_lock:
        return dict(_automap_state)


def _set_state(**updates) -> None:
    with _automap_lock:
        _automap_state.update(updates)


def _automap_stop_requested() -> bool:
    with _automap_lock:
        return bool(_automap_state.get("running")) and bool(_automap_state.get("cancel_requested"))


def stop_automap_all() -> dict:
    with _automap_lock:
        if not _automap_state.get("running"):
            return {"status": "not_running"}
        if _automap_state.get("cancel_requested"):
            return {"status": "already_stopping"}
        requested_at = datetime.now(UTC).isoformat()
        _automap_state.update(cancel_requested=True, last_stop_requested_at=requested_at)
    log_info(logger, "automap.library.stop_requested", "Library automap stop requested")
    publish_library_batch("automap", "stopping")
    return {"status": "stop_requested", "requested_at": requested_at}


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

    if want_dubbed:
        # Accept candidates that are explicitly dubbed OR have Italian audio.
        # The `dub` field is only reliable for V2 API results; scrape-sourced
        # candidates carry no `dub` flag but do have `aw_audio` after enrichment.
        dubbed_or_italian = [candidate for candidate in candidates if bool(candidate.get("dub")) or is_italian(candidate)]
        if dubbed_or_italian:
            return dubbed_or_italian
        return candidates

    # For non-dubbed shows, exclude dubbed/Italian entries.
    # Do NOT further narrow to Japanese-only: Chinese-audio pages (correct for
    # Chinese-origin shows) live in non_dubbed and would be silently dropped if
    # any unrelated Japanese result also exists.  The scoring layer already
    # rewards Japanese audio with +LANGUAGE_WEIGHT so Japanese shows still win.
    non_dubbed = [candidate for candidate in candidates if not bool(candidate.get("dub")) and not is_italian(candidate)]
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

    def fits_marker(
        candidate: dict,
        marker: dict,
        prev_release: datetime | None,
        *,
        is_final_segment: bool,
    ) -> bool:
        count = int(candidate.get("aw_episode_count") or 0)
        target = int(marker.get("count") or 0)
        if abs(count - target) > 1:
            return False
        status = (candidate.get("aw_status") or "").lower()
        if status != "finito" and not (is_final_segment and status == "in corso"):
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
            if not fits_marker(
                candidate,
                markers[index],
                prev_release,
                is_final_segment=index == len(markers) - 1,
            ):
                continue
            chain.append(candidate)
            backtrack(index + 1, chain)
            chain.pop()

    backtrack(0, [])
    return best_chain


def _tiebreak_candidates(season: dict, tied: list[dict], alt_titles: list[str] | None = None) -> dict | None:
    """Secondary scoring pass used when the main gap rule fails.

    Activates only when the best candidate is above the confidence threshold
    but the margin over the second candidate is too small for the gap rule to
    commit.  Applies strict, exact comparisons on already-fetched metadata to
    pick a clear winner among the tied candidates.

    Signals evaluated (all data already present on enriched candidates):
    - Exact episode count match against the manager season count.  Worth more
      points when the AW page is finished (count is final) than when ongoing.
    - Exact release date match (date-level, no tolerance).
    - Status consistency between the manager season and the AW page.
    - AW title exact match against any known alternate title of the show
      (case-insensitive, stripped).  Indicates the AW page was specifically
      named after this show's alternate-title variant.

    Additional signals can be added here later without touching main scoring.

    Returns the winning candidate dict, or None if the tiebreaker cannot
    separate the candidates (falls through to ambiguous as before).

    Note: once this tiebreaker is proven reliable across a wide library the
    gap rule in the caller can be removed — the tiebreaker handles both the
    commit and the ambiguous outcome more precisely.
    """
    mgr_ep = int(season.get("episode_count") or 0)
    mgr_date = str(season.get("air_date_start") or "")[:10]
    mgr_status = (season.get("status") or "").lower()

    # Normalised alternate title set for fast lookup
    alt_set = {t.lower().strip() for t in (alt_titles or [])} if alt_titles else set()

    STATUS_COMPAT = {
        ("ended", "finito"),
        ("continuing", "in corso"),
        ("released", "finito"),
    }

    def score(candidate: dict) -> int:
        pts = 0
        aw_ep = int(candidate.get("aw_episode_count") or 0)
        aw_date = str(candidate.get("aw_release_datetime") or "")[:10]
        aw_status = (candidate.get("aw_status") or "").lower()
        is_finito = aw_status == "finito"

        if mgr_ep and aw_ep:
            if mgr_ep == aw_ep:
                # Finished pages have a final count; weight higher than ongoing
                pts += 3 if is_finito else 1
            else:
                pts -= 1

        if mgr_date and aw_date:
            if mgr_date == aw_date:
                pts += 3
            else:
                pts -= 1

        if (mgr_status, aw_status) in STATUS_COMPAT:
            pts += 1

        # AW page title matches one of the show's known alternate titles exactly
        if alt_set:
            aw_title = (candidate.get("aw_title") or "").lower().strip()
            if aw_title and aw_title in alt_set:
                pts += 2

        return pts

    scored = sorted(tied, key=score, reverse=True)
    if len(scored) >= 2 and score(scored[0]) > score(scored[1]):
        return scored[0]
    return None


def _select_show_winner(season: dict, season_scores: list[dict], alt_titles: list[str] | None = None) -> dict | None:
    if not season_scores:
        return None

    best = season_scores[0]
    if best["confidence_score"] < settings.automap_confidence_threshold:
        return None

    qualified = [
        candidate for candidate in season_scores
        if candidate["confidence_score"] >= settings.automap_confidence_threshold
    ]
    if len(qualified) == 1:
        return best

    second = season_scores[1] if len(season_scores) > 1 else None
    if not second or (best["confidence_score"] - second["confidence_score"]) >= 0.05:
        return best

    tied = [
        candidate for candidate in qualified
        if (best["confidence_score"] - candidate["confidence_score"]) < 0.05
    ]
    return _tiebreak_candidates(season, tied, alt_titles=alt_titles)


def _preaired_type_for_candidate(candidate: dict) -> str:
    try:
        score = float(candidate.get("confidence_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score < settings.automap_confidence_threshold:
        return ""
    if candidate.get("aw_is_placeholder"):
        return "placeholder"
    if not str(candidate.get("aw_link") or "").strip():
        return ""
    if not str(candidate.get("aw_title") or "").strip():
        return ""
    try:
        episode_count = int(candidate.get("aw_episode_count") or 0)
    except (TypeError, ValueError):
        episode_count = 0
    if episode_count <= 0:
        return ""
    if not candidate.get("aw_release_datetime"):
        return ""
    if not str(candidate.get("aw_status") or "").strip():
        return ""
    return "prereleased"


def _preaired_factors(candidate: dict, preaired_type: str) -> dict:
    factors = dict(candidate.get("confidence_factors") or {})
    factors["preaired"] = True
    factors["preaired_type"] = preaired_type
    factors["preaired_placeholder"] = preaired_type == "placeholder"
    factors["preaired_prereleased"] = preaired_type == "prereleased"
    return factors


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
    if _automap_stop_requested():
        return {"status": "cancelled", "movie_id": movie_id}
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


def automap_movie_preview(movie_id: int) -> dict:
    movie = get_movie_detail(movie_id)
    if not movie:
        return {"status": "error", "message": "movie_not_found", "movie_id": movie_id}

    want_dubbed = resolve_movie_language_preference(movie)
    candidates = []
    for candidate in _filter_language_candidates(
        _discovery_candidates(
            movie["title"],
            [item.get("title", "") for item in movie.get("alternate_titles", [])],
        ),
        want_dubbed,
    ):
        score, factors = calculate_movie_confidence(movie, candidate, want_dubbed=want_dubbed)
        candidates.append({**candidate, "confidence_score": score, "confidence_factors": factors})
    candidates.sort(key=lambda item: item["confidence_score"], reverse=True)
    return {
        "status": "preview",
        "movie_id": movie_id,
        "threshold": settings.automap_movie_confidence_threshold,
        "mapping": candidates[0] if candidates else None,
        "candidates": candidates[:10],
    }


def automap_show(show_id: int, season_number: int | None = None, force: bool = False, *, emit_logs: bool = True) -> dict:
    if _automap_stop_requested():
        return {
            "status": "cancelled",
            "show_id": show_id,
            "mapped_seasons": [],
            "ambiguous": [],
            "candidates": [],
            "ignored_seasons": [],
        }
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
        if _automap_stop_requested():
            return {
                "status": "cancelled",
                "show_id": show_id,
                "mapped_seasons": sorted(mapped_seasons),
                "ambiguous": ambiguous,
                "candidates": scored_candidates[:10],
                "ignored_seasons": sorted(ignored_seasons),
            }
        sn = season["season_number"]
        if sn in handled:
            continue

        season_scores = _build_scored_candidates(show, season, candidates, want_dubbed, reserved_links)
        if season_scores:
            scored_candidates.extend(
                [{**candidate, "season_number": sn} for candidate in season_scores[:3]]
            )

        target_count = int(season.get("episode_count") or 0)
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
                {**candidate, "preaired_type": preaired_type}
                for candidate in season_scores
                if (preaired_type := _preaired_type_for_candidate(candidate))
            ]
            if preaired:
                best_preaired = preaired[0]
                preaired_type = str(best_preaired["preaired_type"])
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
                            "confidence_factors": json.dumps(_preaired_factors(best_preaired, preaired_type)),
                            "linked_with_season": None,
                        }
                    ],
                )
                mapped_seasons.append(sn)
                handled.add(sn)
                reserved_links.add(best_preaired["aw_link"])
                continue
            # Unaired seasons should never degrade into ambiguous "needs review".
            # We either map a safe preair candidate, or skip silently until a
            # placeholder/prereleased page with enough metadata appears.
            skipped_unaired.append(sn)
            continue

        show_alt_titles = [row["title"] for row in show.get("alternate_titles", [])]
        commit_winner = _select_show_winner(season, season_scores, alt_titles=show_alt_titles)
        if not commit_winner:
            ambiguous.append({"season": sn, "candidates": season_scores[:5]})
            continue

        if int(commit_winner.get("aw_episode_count") or 0) >= target_count > 0:
            _propagate_single_link(
                show_id=show_id,
                eligible_seasons=eligible_seasons,
                start_index=index,
                best=commit_winner,
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
                    "aw_link": commit_winner["aw_link"],
                    "aw_title": commit_winner["aw_title"],
                    "aw_episode_count": commit_winner["aw_episode_count"],
                    "aw_total_episodes": commit_winner["aw_total_episodes"],
                    "aw_status": commit_winner.get("aw_status", ""),
                    "aw_category": commit_winner.get("aw_category", ""),
                    "confidence_score": commit_winner["confidence_score"],
                    "confidence_factors": json.dumps(commit_winner["confidence_factors"]),
                    "linked_with_season": None,
                }
            ],
        )
        mapped_seasons.append(sn)
        handled.add(sn)
        reserved_links.add(commit_winner["aw_link"])

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


def automap_show_preview(show_id: int, season_number: int | None = None) -> dict:
    show = get_show_detail(show_id)
    if not show:
        return {
            "status": "error",
            "message": "show_not_found",
            "show_id": show_id,
            "candidates": [],
            "ignored_seasons": [],
        }

    want_dubbed = resolve_show_language_preference(show)
    scored_candidates: list[dict] = []
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
        return {
            "status": "preview",
            "show_id": show_id,
            "candidates": [],
            "ignored_seasons": sorted(ignored_seasons),
        }

    candidates = _discovery_candidates(show["title"], _show_alternate_titles(show))
    reserved_links = _existing_links_by_show(show)

    for season in target_seasons:
        sn = season["season_number"]
        season_scores = _build_scored_candidates(show, season, candidates, want_dubbed, reserved_links)
        if season_scores:
            scored_candidates.extend(
                [{**candidate, "season_number": sn} for candidate in season_scores[:3]]
            )

    return {
        "status": "preview",
        "show_id": show_id,
        "candidates": scored_candidates[:15],
        "ignored_seasons": sorted(ignored_seasons),
    }


def _run_background(target, *args, **kwargs) -> dict:
    with _automap_lock:
        if _automap_state.get("running"):
            return {"status": "already_running"}
        _automap_state.update(
            running=True,
            cancel_requested=False,
            last_started_at=datetime.now(UTC).isoformat(),
            last_stop_requested_at=None,
            last_error="",
        )

    def worker():
        try:
            result = target(*args, **kwargs)
            _set_state(last_result=result)
            if target is automap_all:
                summary = _summarize_automap_all(result)
                if result.get("cancelled"):
                    log_info(
                        logger,
                        "automap.library.cancelled",
                        "Library automap stopped",
                        lines=[
                            f"shows={summary['shows']}",
                            f"movies={summary['movies']}",
                            f"mapped_show_seasons={summary['mapped_show_seasons']}",
                            f"mapped_movies={summary['mapped_movies']}",
                        ],
                        details=summary,
                    )
                    publish_library_batch("automap", "cancelled", **summary)
                else:
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
            _set_state(running=False, cancel_requested=False, last_finished_at=datetime.now(UTC).isoformat())

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
        if _automap_stop_requested():
            return {"status": "cancelled", "cancelled": True, "shows": show_results, "movies": movie_results}
        if kind == "show":
            result = automap_show(item_id, force=force)
            if result.get("status") == "cancelled":
                if result.get("mapped_seasons") or result.get("ambiguous"):
                    show_results.append(result)
                return {"status": "cancelled", "cancelled": True, "shows": show_results, "movies": movie_results}
            show_results.append(result)
        else:
            result = automap_movie(item_id, force=force)
            if result.get("status") == "cancelled":
                return {"status": "cancelled", "cancelled": True, "shows": show_results, "movies": movie_results}
            movie_results.append(result)
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
