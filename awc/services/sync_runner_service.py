"""Manager sync routines for the clean rebuild."""

from datetime import UTC, datetime
import json
import threading

from ..core.config import settings
from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.library_write import (
    prune_missing_movies,
    prune_missing_shows,
    replace_movie_alternate_titles,
    replace_scene_episode_map,
    replace_show_alternate_titles,
    replace_show_seasons,
    set_sync_meta,
    upsert_movie,
    upsert_show,
)

_sync_lock = threading.Lock()
_sync_status = {"running": False, "last_started_at": None, "last_finished_at": None}


def _language_name(payload) -> str | None:
    if isinstance(payload, dict):
        return payload.get("name")
    if isinstance(payload, str):
        return payload
    return None


def _genres_json(value) -> str | None:
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return None


def _build_show_payload(series: dict) -> dict:
    return {
        "sonarr_id": series.get("id"),
        "tvdb_id": series.get("tvdbId"),
        "title": series.get("title"),
        "sort_title": series.get("sortTitle"),
        "series_type": series.get("seriesType", "standard"),
        "monitored": series.get("monitored", True),
        "status": series.get("status"),
        "year": series.get("year"),
        "original_language": _language_name(series.get("originalLanguage")) or "ja",
        "first_aired": series.get("firstAired"),
        "genres": _genres_json(series.get("genres")),
    }


def _build_show_seasons(series: dict, episodes: list[dict]) -> list[dict]:
    by_season: dict[int, dict] = {}
    for season in series.get("seasons") or []:
        number = season.get("seasonNumber")
        if number is None:
            continue
        stats = season.get("statistics") or {}
        by_season[number] = {
            "season_number": number,
            "monitored": season.get("monitored", False),
            "episode_count": stats.get("totalEpisodeCount", 0),
            "air_date_start": None,
            "air_date_end": None,
        }

    for episode in episodes:
        season_number = episode.get("seasonNumber")
        air_date = episode.get("airDate") or episode.get("airDateUtc")
        if season_number is None or not air_date:
            continue
        air_date = str(air_date).split("T")[0]
        season = by_season.setdefault(
            season_number,
            {
                "season_number": season_number,
                "monitored": True,
                "episode_count": 0,
                "air_date_start": air_date,
                "air_date_end": air_date,
            },
        )
        if not season.get("air_date_start") or air_date < season["air_date_start"]:
            season["air_date_start"] = air_date
        if not season.get("air_date_end") or air_date > season["air_date_end"]:
            season["air_date_end"] = air_date
    return list(sorted(by_season.values(), key=lambda item: item["season_number"]))


def _build_show_alt_titles(series: dict) -> list[dict]:
    items = []
    for title in series.get("alternateTitles") or []:
        raw = title.get("title") or title.get("sourceTitle")
        if not raw:
            continue
        items.append(
            {
                "title": raw,
                "source": "sonarr",
                "title_type": title.get("titleType"),
                "language": _language_name(title.get("language")),
                "scene_season_number": title.get("seasonNumber"),
                "title_year": title.get("year"),
            }
        )
    return items


def _build_scene_episode_map(episodes: list[dict]) -> list[dict]:
    items = []
    for episode in episodes:
        scene_season = episode.get("sceneSeasonNumber")
        scene_episode = episode.get("sceneEpisodeNumber")
        internal_season = episode.get("seasonNumber")
        internal_episode = episode.get("episodeNumber")
        if None in (scene_season, scene_episode, internal_season, internal_episode):
            continue
        items.append(
            {
                "scene_season": scene_season,
                "scene_episode": scene_episode,
                "internal_season": internal_season,
                "internal_episode": internal_episode,
                "absolute_episode": episode.get("absoluteEpisodeNumber"),
            }
        )
    return items


def sync_single_show(series_id: int) -> int | None:
    client = SonarrClient()
    if not client.is_configured():
        return None
    detail = client.fetch_series_detail(series_id)
    if not detail:
        return None
    episodes = client.fetch_episodes(series_id)
    show_id = upsert_show(_build_show_payload(detail))
    replace_show_seasons(show_id, _build_show_seasons(detail, episodes))
    replace_show_alternate_titles(show_id, _build_show_alt_titles(detail))
    replace_scene_episode_map(show_id, _build_scene_episode_map(episodes))
    set_sync_meta("last_sonarr_sync", datetime.now(UTC).isoformat())
    return show_id


def sync_sonarr_library() -> int:
    client = SonarrClient()
    if not client.is_configured():
        return 0
    tags = client.fetch_tags()
    anime_tag = settings.sonarr_anime_tag.lower()
    anime_tag_id = next((tag_id for tag_id, label in tags.items() if label.lower() == anime_tag), None)

    processed = 0
    seen: set[int] = set()
    for series in client.fetch_series():
        if anime_tag_id is not None and anime_tag_id not in (series.get("tags") or []):
            continue
        if sync_single_show(series["id"]):
            processed += 1
            seen.add(series["id"])
    if seen:
        prune_missing_shows(seen)
    set_sync_meta("last_sonarr_sync", datetime.now(UTC).isoformat())
    return processed


def _build_movie_payload(movie: dict) -> dict:
    return {
        "radarr_id": movie.get("id"),
        "tmdb_id": movie.get("tmdbId"),
        "imdb_id": movie.get("imdbId"),
        "title": movie.get("title"),
        "sort_title": movie.get("sortTitle"),
        "monitored": movie.get("monitored", True),
        "status": movie.get("status"),
        "year": movie.get("year"),
        "original_language": _language_name(movie.get("originalLanguage")),
        "first_aired": movie.get("physicalRelease") or movie.get("digitalRelease") or movie.get("inCinemas"),
        "genres": _genres_json(movie.get("genres")),
    }


def _build_movie_alt_titles(movie: dict) -> list[dict]:
    items = []
    for title in movie.get("alternateTitles") or []:
        raw = title.get("title") or title.get("sourceTitle")
        if not raw:
            continue
        items.append(
            {
                "title": raw,
                "source": "radarr",
                "language": _language_name(title.get("language")),
            }
        )
    return items


def sync_single_movie(movie_payload_or_id) -> int | None:
    client = RadarrClient()
    if not client.is_configured():
        return None
    if isinstance(movie_payload_or_id, dict):
        movie = movie_payload_or_id
    else:
        movie = None
        for candidate in client.fetch_movies():
            if candidate.get("id") == movie_payload_or_id:
                movie = candidate
                break
    if not movie:
        return None
    movie_id = upsert_movie(_build_movie_payload(movie))
    replace_movie_alternate_titles(movie_id, _build_movie_alt_titles(movie))
    set_sync_meta("last_radarr_sync", datetime.now(UTC).isoformat())
    return movie_id


def sync_radarr_library() -> int:
    client = RadarrClient()
    if not client.is_configured():
        return 0
    tags = client.fetch_tags()
    anime_tag = settings.anime_tag.lower()
    anime_tag_id = next((tag_id for tag_id, label in tags.items() if label.lower() == anime_tag), None)

    processed = 0
    seen: set[int] = set()
    for movie in client.fetch_movies():
        if anime_tag_id is not None and anime_tag_id not in (movie.get("tags") or []):
            continue
        if sync_single_movie(movie):
            processed += 1
            seen.add(movie["id"])
    if seen:
        prune_missing_movies(seen)
    set_sync_meta("last_radarr_sync", datetime.now(UTC).isoformat())
    return processed


def sync_all() -> dict:
    with _sync_lock:
        _sync_status["running"] = True
        _sync_status["last_started_at"] = datetime.now(UTC).isoformat()
        try:
            sonarr_count = sync_sonarr_library()
            radarr_count = sync_radarr_library()
            return {"sonarr": sonarr_count, "radarr": radarr_count}
        finally:
            _sync_status["running"] = False
            _sync_status["last_finished_at"] = datetime.now(UTC).isoformat()


def sync_status() -> dict:
    return dict(_sync_status)


def sync_now_sonarr() -> dict:
    with _sync_lock:
        _sync_status["running"] = True
        _sync_status["last_started_at"] = datetime.now(UTC).isoformat()
        try:
            sonarr_count = sync_sonarr_library()
            return {"sonarr": sonarr_count, "radarr": 0}
        finally:
            _sync_status["running"] = False
            _sync_status["last_finished_at"] = datetime.now(UTC).isoformat()


def sync_now_radarr() -> dict:
    with _sync_lock:
        _sync_status["running"] = True
        _sync_status["last_started_at"] = datetime.now(UTC).isoformat()
        try:
            radarr_count = sync_radarr_library()
            return {"sonarr": 0, "radarr": radarr_count}
        finally:
            _sync_status["running"] = False
            _sync_status["last_finished_at"] = datetime.now(UTC).isoformat()
