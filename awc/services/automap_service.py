"""Automatic mapping workflows for shows and movies."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import threading

from ..core.config import settings
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

_automap_lock = threading.Lock()
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


def _show_alternate_titles(show: dict) -> list[str]:
    return [item.get("title", "") for item in show.get("alternate_titles", []) if item.get("title")]


def _discovery_candidates(title: str, alternates: list[str], limit: int = 12) -> list[dict]:
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


def _build_scored_candidates(show: dict, season: dict, candidates: list[dict], want_dubbed: bool, reserved_links: set[str]) -> list[dict]:
    own_links = {
        (mapping.get("aw_link") or "").strip()
        for mapping in season.get("mappings", [])
        if mapping.get("aw_link")
    }
    blocked_links = reserved_links - own_links
    scored: list[dict] = []
    for candidate in candidates:
        if candidate.get("aw_link", "") in blocked_links:
            continue
        score, factors = calculate_show_confidence(show, season, candidate, want_dubbed=want_dubbed)
        scored.append({**candidate, "confidence_score": score, "confidence_factors": factors})
    scored.sort(key=lambda item: item["confidence_score"], reverse=True)
    return scored


def _detect_split_cour_pair(show: dict, season: dict, season_scores: list[dict], want_dubbed: bool) -> tuple[list[dict], float, dict] | None:
    target_count = int(season.get("episode_count") or 0)
    if not target_count:
        return None

    season_end = season.get("air_date_end") or ""
    best_pair: tuple[list[dict], float, dict] | None = None

    for first in season_scores:
        first_count = int(first.get("aw_episode_count") or 0)
        if not first_count or first_count >= target_count:
            continue
        if (first.get("aw_status") or "").lower() != "finito":
            continue
        first_release = first.get("aw_release_datetime")

        for second in season_scores:
            if first["aw_link"] == second["aw_link"]:
                continue
            second_count = int(second.get("aw_episode_count") or 0)
            if not second_count:
                continue
            if (first.get("aw_audio") or "").lower() != (second.get("aw_audio") or "").lower():
                continue
            second_release = second.get("aw_release_datetime")
            if first_release and second_release and second_release <= first_release:
                continue
            if season_end and second_release:
                try:
                    last_aired_dt = datetime.fromisoformat(season_end.replace("Z", "+00:00"))
                    if second_release > last_aired_dt:
                        delta_days = (second_release - last_aired_dt).days
                        if delta_days > 30:
                            continue
                except (TypeError, ValueError):
                    pass
            combined = first_count + second_count
            if abs(combined - target_count) > 1:
                continue

            combined_candidate = {
                **first,
                "aw_title": f"{first.get('aw_title', '')} + {second.get('aw_title', '')}",
                "aw_episode_count": combined,
                "aw_total_episodes": int(first.get("aw_total_episodes") or first_count) + int(second.get("aw_total_episodes") or second_count),
                "aw_release_datetime": first_release,
            }
            combined_score, combined_factors = calculate_show_confidence(
                show, season, combined_candidate, want_dubbed=want_dubbed
            )
            if combined_score < settings.automap_confidence_threshold:
                continue
            if best_pair is None or combined_score > best_pair[1]:
                best_pair = ([first, second], combined_score, combined_factors)

    return best_pair


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
    if available <= 0:
        return

    chain: list[dict] = []
    consumed = 0
    for season in eligible_seasons[start_index:]:
        sn = season["season_number"]
        if sn in handled:
            continue
        season_count = int(season.get("episode_count") or 0)
        if season_count <= 0:
            break
        if consumed + season_count > available:
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


def automap_movie(movie_id: int, force: bool = False) -> dict:
    movie = get_movie_detail(movie_id)
    if not movie:
        return {"status": "error", "message": "movie_not_found", "movie_id": movie_id}
    if movie.get("mapping") and not force:
        return {"status": "already_mapped", "movie_id": movie_id}

    want_dubbed = resolve_movie_language_preference(movie)
    candidates = []
    for candidate in _discovery_candidates(
        movie["title"],
        [item.get("title", "") for item in movie.get("alternate_titles", [])],
    ):
        score, factors = calculate_movie_confidence(movie, candidate, want_dubbed=want_dubbed)
        candidates.append({**candidate, "confidence_score": score, "confidence_factors": factors})
    candidates.sort(key=lambda item: item["confidence_score"], reverse=True)

    if not candidates or candidates[0]["confidence_score"] < settings.automap_movie_confidence_threshold:
        if force:
            remove_movie_mapping(movie_id)
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
    return {"status": "success", "movie_id": movie_id, "mapping": mapping, "candidates": candidates[:5]}


def automap_show(show_id: int, season_number: int | None = None, force: bool = False) -> dict:
    show = get_show_detail(show_id)
    if not show:
        return {"status": "error", "message": "show_not_found", "show_id": show_id}

    want_dubbed = resolve_show_language_preference(show)
    candidates = _discovery_candidates(show["title"], _show_alternate_titles(show))
    scored_candidates: list[dict] = []
    mapped_seasons: list[int] = []
    ambiguous: list[dict] = []
    handled: set[int] = set()

    target_seasons = [
        season for season in show.get("seasons", [])
        if season.get("season_number", 0) > 0 and (season_number is None or season["season_number"] == season_number)
    ]
    if not target_seasons:
        return {"status": "not_found", "show_id": show_id, "mapped_seasons": [], "ambiguous": [], "candidates": []}

    eligible_seasons = [
        season for season in target_seasons
        if force or not list_show_mappings(show_id, season["season_number"])
    ]
    if not eligible_seasons:
        return {"status": "already_mapped", "show_id": show_id, "mapped_seasons": [], "ambiguous": [], "candidates": []}

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

        split_pair = _detect_split_cour_pair(show, season, season_scores, want_dubbed)
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

        if best and best["confidence_score"] >= settings.automap_confidence_threshold and (
            not second or (best["confidence_score"] - second["confidence_score"]) >= 0.05
        ):
            _propagate_single_link(
                show_id=show_id,
                eligible_seasons=eligible_seasons,
                start_index=index,
                best=best,
                mapped_seasons=mapped_seasons,
                handled=handled,
                reserved_links=reserved_links,
            )
            continue

        ambiguous.append({"season": sn, "candidates": season_scores[:5]})

    status = "success" if mapped_seasons else "not_found"
    if ambiguous and mapped_seasons:
        status = "partial"
    elif ambiguous and not mapped_seasons:
        status = "ambiguous"

    return {
        "status": status,
        "show_id": show_id,
        "mapped_seasons": sorted(mapped_seasons),
        "ambiguous": ambiguous,
        "candidates": scored_candidates[:10],
    }


def _run_background(target, *args, **kwargs) -> dict:
    def worker():
        _set_state(running=True, last_started_at=datetime.now(UTC).isoformat(), last_error="")
        try:
            result = target(*args, **kwargs)
            _set_state(last_result=result)
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
        show_ids = [row[0] for row in conn.execute("SELECT id FROM shows ORDER BY title COLLATE NOCASE").fetchall()]
    results = [automap_show(show_id, force=force) for show_id in show_ids]
    return {"shows": results}


def automap_all_movies(force: bool = False) -> dict:
    with get_db() as conn:
        movie_ids = [row[0] for row in conn.execute("SELECT id FROM movies ORDER BY title COLLATE NOCASE").fetchall()]
    results = [automap_movie(movie_id, force=force) for movie_id in movie_ids]
    return {"movies": results}


def start_automap_all(force: bool = False) -> dict:
    return _run_background(automap_all, force)


def start_automap_all_movies(force: bool = False) -> dict:
    return _run_background(automap_all_movies, force)
