"""Shared webhook normalization for Sonarr and Radarr parity."""

from ..domain.media import MediaManager
from ..repositories.movies import find_movie_by_manager_identity
from ..repositories.shows import find_show_by_manager_identity


def _detect_manager(payload: dict) -> MediaManager | None:
    if "series" in payload:
        return MediaManager.SONARR
    if "movie" in payload:
        return MediaManager.RADARR
    return None


def _extract_entity(payload: dict, manager: MediaManager | None) -> dict | None:
    if manager is MediaManager.SONARR:
        series = payload.get("series") or {}
        return {
            "id": series.get("id"),
            "title": series.get("title"),
            "tvdb_id": series.get("tvdbId"),
            "imdb_id": series.get("imdbId"),
            "tmdb_id": series.get("tmdbId"),
            "path": series.get("path"),
        }
    if manager is MediaManager.RADARR:
        movie = payload.get("movie") or {}
        return {
            "id": movie.get("id"),
            "title": movie.get("title"),
            "year": movie.get("year"),
            "imdb_id": movie.get("imdbId"),
            "tmdb_id": movie.get("tmdbId"),
            "path": movie.get("folderPath") or movie.get("path"),
        }
    return None


def _classify_event(event_type: str) -> str:
    normalized = event_type.lower()
    if "test" in normalized:
        return "test"
    if "delete" in normalized:
        return "delete"
    if "import" in normalized or "download" in normalized:
        return "import"
    if "grab" in normalized:
        return "grab"
    if "rename" in normalized or "file" in normalized:
        return "file"
    if "add" in normalized:
        return "add"
    return "unknown"


def _resolve_local_match(manager: MediaManager | None, entity: dict | None) -> dict | None:
    if not manager or not entity:
        return None

    if manager is MediaManager.SONARR:
        match = find_show_by_manager_identity(
            sonarr_id=entity.get("id"),
            tvdb_id=entity.get("tvdb_id"),
            title=entity.get("title") or "",
        )
    else:
        match = find_movie_by_manager_identity(
            radarr_id=entity.get("id"),
            tmdb_id=entity.get("tmdb_id"),
            imdb_id=entity.get("imdb_id") or "",
            title=entity.get("title") or "",
        )

    if not match:
        return None

    return {
        "id": match.get("id"),
        "title": match.get("title"),
        "manager_id": match.get("sonarr_id") if manager is MediaManager.SONARR else match.get("radarr_id"),
        "matched_by": (
            "manager_id"
            if entity.get("id") and entity.get("id") == (match.get("sonarr_id") if manager is MediaManager.SONARR else match.get("radarr_id"))
            else "external_id"
            if (
                manager is MediaManager.SONARR
                and entity.get("tvdb_id")
                and entity.get("tvdb_id") == match.get("tvdb_id")
            )
            or (
                manager is MediaManager.RADARR
                and (
                    (entity.get("tmdb_id") and entity.get("tmdb_id") == match.get("tmdb_id"))
                    or (entity.get("imdb_id") and entity.get("imdb_id") == match.get("imdb_id"))
                )
            )
            else "title"
        ),
    }


def normalize_webhook(payload: dict) -> dict:
    manager = _detect_manager(payload)
    event_type = (payload.get("eventType") or payload.get("event") or "").strip()
    entity = _extract_entity(payload, manager)
    local_match = _resolve_local_match(manager, entity)

    return {
        "accepted": bool(manager and event_type),
        "manager": manager.value if manager else None,
        "event_type": event_type or None,
        "event_family": _classify_event(event_type) if event_type else None,
        "entity": entity,
        "local_match": local_match,
        "series": payload.get("series") if manager is MediaManager.SONARR else None,
        "movie": payload.get("movie") if manager is MediaManager.RADARR else None,
    }
