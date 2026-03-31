"""Automatic mapping workflows for shows and movies."""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher
import json
import re
import threading

from ..core.config import settings
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
from ..repositories.mappings import (
    list_show_mappings,
    remove_movie_mapping,
    replace_movie_mapping_auto,
    replace_show_mappings_auto,
)
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail

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


def _normalize_title(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\b(season|part|movie|film|the|and|of|cour|tv|ova|ona)\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _extract_year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _candidate_meta(result: dict) -> dict:
    client = AnimeWorldClient()
    info, episodes = client.get_info_and_episodes(result.get("slug") or result.get("url", ""))
    non_special, total, _ = client.count_non_special_episodes(episodes)
    return {
        "aw_link": client.url_to_slug(result.get("url") or result.get("slug", "")),
        "aw_title": result.get("title", ""),
        "aw_jtitle": result.get("japanese_title", ""),
        "aw_status": str(info.get("Stato") or info.get("status") or ""),
        "aw_category": str(info.get("Categoria") or info.get("category") or result.get("kind") or ""),
        "aw_audio": str(info.get("Audio") or info.get("audio") or ""),
        "aw_year": _extract_year(info.get("Data di Uscita") or info.get("release_date")),
        "aw_episode_count": non_special,
        "aw_total_episodes": total,
    }


def _show_alternate_titles(show: dict) -> list[str]:
    return [item.get("title", "") for item in show.get("alternate_titles", []) if item.get("title")]


def _show_score(show: dict, season: dict, candidate: dict) -> tuple[float, dict]:
    titles = [show.get("title", ""), * _show_alternate_titles(show)]
    title_score = max(_similarity(title, candidate["aw_title"]) for title in titles if title) if titles else 0.0
    if candidate.get("aw_jtitle"):
        title_score = max(title_score, max(_similarity(title, candidate["aw_jtitle"]) for title in titles if title))

    year_score = 0.0
    show_year = show.get("year")
    if show_year and candidate.get("aw_year"):
        diff = abs(int(show_year) - int(candidate["aw_year"]))
        year_score = 1.0 if diff == 0 else 0.5 if diff == 1 else 0.0

    target_count = int(season.get("episode_count") or 0)
    aw_count = int(candidate.get("aw_episode_count") or 0)
    count_score = 0.0
    if target_count and aw_count:
        if target_count == aw_count:
            count_score = 1.0
        else:
            diff_ratio = abs(target_count - aw_count) / max(target_count, aw_count)
            count_score = max(0.0, 1.0 - diff_ratio)

    total = round((title_score * 0.55) + (year_score * 0.2) + (count_score * 0.25), 4)
    return total, {
        "title": round(title_score, 4),
        "year": round(year_score, 4),
        "episode_count": round(count_score, 4),
    }


def _movie_score(movie: dict, candidate: dict) -> tuple[float, dict]:
    titles = [movie.get("title", "")] + [item.get("title", "") for item in movie.get("alternate_titles", [])]
    title_score = max(_similarity(title, candidate["aw_title"]) for title in titles if title) if titles else 0.0
    if candidate.get("aw_jtitle"):
        title_score = max(title_score, max(_similarity(title, candidate["aw_jtitle"]) for title in titles if title))

    year_score = 0.0
    movie_year = movie.get("year")
    if movie_year and candidate.get("aw_year"):
        diff = abs(int(movie_year) - int(candidate["aw_year"]))
        year_score = 1.0 if diff == 0 else 0.5 if diff == 1 else 0.0

    total = round((title_score * 0.8) + (year_score * 0.2), 4)
    return total, {
        "title": round(title_score, 4),
        "year": round(year_score, 4),
    }


def _discovery_candidates(title: str, alternates: list[str], limit: int = 12) -> list[dict]:
    client = AnimeWorldClient()
    results = []
    seen: set[str] = set()
    for query in [title, *alternates]:
        if not query:
            continue
        for result in client.search(query, limit=limit):
            slug = client.url_to_slug(result.get("url") or result.get("slug", ""))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            meta = _candidate_meta(result)
            results.append({**result, **meta})
        if len(results) >= limit:
            break
    return results[:limit]


def automap_movie(movie_id: int, force: bool = False) -> dict:
    movie = get_movie_detail(movie_id)
    if not movie:
        return {"status": "error", "message": "movie_not_found", "movie_id": movie_id}
    if movie.get("mapping") and not force:
        return {"status": "already_mapped", "movie_id": movie_id}

    candidates = []
    for candidate in _discovery_candidates(
        movie["title"],
        [item.get("title", "") for item in movie.get("alternate_titles", [])],
    ):
        score, factors = _movie_score(movie, candidate)
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

    for season in eligible_seasons:
        sn = season["season_number"]
        if sn in handled:
            continue

        season_scores = []
        for candidate in candidates:
            score, factors = _show_score(show, season, candidate)
            season_scores.append({**candidate, "confidence_score": score, "confidence_factors": factors})
        season_scores.sort(key=lambda item: item["confidence_score"], reverse=True)
        if season_scores:
            scored_candidates.extend(
                [{**candidate, "season_number": sn} for candidate in season_scores[:3]]
            )

        target_count = int(season.get("episode_count") or 0)
        best = season_scores[0] if season_scores else None
        second = season_scores[1] if len(season_scores) > 1 else None

        if best and best["confidence_score"] >= settings.automap_confidence_threshold and (
            not second or (best["confidence_score"] - second["confidence_score"]) >= 0.05
        ):
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

            next_season = next((item for item in eligible_seasons if item["season_number"] == sn + 1), None)
            best_count = int(best.get("aw_episode_count") or 0)
            if next_season and next_season["season_number"] not in handled:
                combined = target_count + int(next_season.get("episode_count") or 0)
                if combined and best_count >= combined:
                    replace_show_mappings_auto(
                        show_id=show_id,
                        season_number=next_season["season_number"],
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
                                "confidence_factors": json.dumps({**best["confidence_factors"], "linked": sn}),
                                "linked_with_season": sn,
                            }
                        ],
                    )
                    mapped_seasons.append(next_season["season_number"])
                    handled.add(next_season["season_number"])
            continue

        split_pair = None
        if target_count:
            for first in season_scores:
                for second_candidate in season_scores:
                    if first["aw_link"] == second_candidate["aw_link"]:
                        continue
                    combined = int(first.get("aw_episode_count") or 0) + int(second_candidate.get("aw_episode_count") or 0)
                    if combined == target_count and min(first["confidence_score"], second_candidate["confidence_score"]) >= 0.65:
                        split_pair = [first, second_candidate]
                        break
                if split_pair:
                    break

        if split_pair:
            replace_show_mappings_auto(
                show_id=show_id,
                season_number=sn,
                items=[
                    {
                        "part": index,
                        "aw_link": candidate["aw_link"],
                        "aw_title": candidate["aw_title"],
                        "aw_episode_count": candidate["aw_episode_count"],
                        "aw_total_episodes": candidate["aw_total_episodes"],
                        "aw_status": candidate.get("aw_status", ""),
                        "aw_category": candidate.get("aw_category", ""),
                        "confidence_score": candidate["confidence_score"],
                        "confidence_factors": json.dumps({**candidate["confidence_factors"], "split_cour": True}),
                        "linked_with_season": None,
                    }
                    for index, candidate in enumerate(split_pair, start=1)
                ],
            )
            mapped_seasons.append(sn)
            handled.add(sn)
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
