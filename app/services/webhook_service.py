"""Shared webhook normalization and handling for Sonarr and Radarr."""

from ..core.config import settings
from ..domain.media import MediaManager
from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.library_write import delete_movie_by_radarr_id, delete_show_by_sonarr_id
from ..repositories.movies import find_movie_by_manager_identity
from ..repositories.shows import find_show_by_manager_identity
from .automap_service import automap_movie, automap_show
from .download_service import find_completed_download_for_import_webhook, mark_imported
from .sync_runner_service import sync_single_movie, sync_single_show


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
    normalized = _event_key(event_type)
    if "test" in normalized:
        return "test"
    if "health" in normalized:
        return "health"
    if "manual" in normalized:
        return "manual"
    if "application" in normalized or "update" in normalized:
        return "application"
    if "import" in normalized or "download" in normalized:
        return "import"
    if "grab" in normalized:
        return "grab"
    if "rename" in normalized or "file" in normalized:
        return "file"
    if _is_delete_event_key(normalized):
        return "delete"
    if "add" in normalized:
        return "add"
    return "unknown"


def _event_key(event_type: str) -> str:
    return "".join(ch for ch in str(event_type or "").strip().lower() if ch.isalnum())


def _is_add_event_key(event_key: str) -> bool:
    prefixes = ("series", "movie", "onseries", "onmovie")
    return event_key.startswith(prefixes) and event_key.endswith(("add", "added"))


def _is_delete_event_key(event_key: str) -> bool:
    if "file" in event_key:
        return False
    prefixes = ("series", "movie", "onseries", "onmovie")
    return event_key.startswith(prefixes) and event_key.endswith(("delete", "deleted"))


def is_add_webhook(normalized: dict) -> bool:
    return _is_add_event_key(_event_key(str(normalized.get("event_type") or "")))


def is_delete_webhook(normalized: dict) -> bool:
    return _is_delete_event_key(_event_key(str(normalized.get("event_type") or "")))


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
        "manager_entity_id": entity.get("id") if entity else None,
        "series": payload.get("series") if manager is MediaManager.SONARR else None,
        "movie": payload.get("movie") if manager is MediaManager.RADARR else None,
    }


def _sonarr_webhook_episode_ids(payload: dict) -> list[int]:
    episode_ids: list[int] = []
    for episode in payload.get("episodes") or []:
        try:
            episode_ids.append(int(episode.get("id")))
        except (TypeError, ValueError):
            continue
    return episode_ids


def _sonarr_webhook_series_and_season(payload: dict) -> tuple[int | None, int | None]:
    series = payload.get("series") or {}
    series_id = series.get("id")
    season_number = None
    for episode in payload.get("episodes") or []:
        try:
            season_number = int(episode.get("seasonNumber"))
            break
        except (TypeError, ValueError):
            continue
    return (int(series_id) if series_id is not None else None, season_number)


def handle_import_webhook(normalized: dict, payload: dict) -> dict:
    manager = str(normalized.get("manager") or "")
    manager_entity_id = normalized.get("manager_entity_id")
    if manager not in {"sonarr", "radarr"} or manager_entity_id is None:
        return {"handled": False, "result": "unsupported"}

    manager_entity_id = int(manager_entity_id)
    matched = find_completed_download_for_import_webhook(manager, manager_entity_id, payload)
    if not matched:
        return {
            "handled": True,
            "result": "no_match",
            "lines": ["result=no completed download match"],
        }

    updated = mark_imported(str(matched.get("id") or ""), emit_log=False)
    if not updated:
        return {
            "handled": True,
            "result": "already_settled",
            "lines": [
                f"download={matched.get('filename') or matched.get('id')}",
                "result=already settled",
            ],
        }

    lines = [f"download={updated.get('filename') or updated.get('id')}", "result=imported"]

    if settings.unmonitor_imported:
        if manager == "sonarr":
            sonarr_client = SonarrClient()
            episode_ids = _sonarr_webhook_episode_ids(payload)
            unmonitored = 0
            for episode_id in episode_ids:
                if sonarr_client.unmonitor_episode(episode_id):
                    unmonitored += 1
            if episode_ids:
                lines.append(f"unmonitored={unmonitored}/{len(episode_ids)}")

                series_id, season_number = _sonarr_webhook_series_and_season(payload)
                if series_id is not None and season_number is not None:
                    season_episodes = sonarr_client.fetch_season_episodes(series_id, season_number)
                    if season_episodes and all(not ep.get("monitored") for ep in season_episodes):
                        if sonarr_client.unmonitor_season(series_id, season_number):
                            lines.append(f"season_unmonitored={season_number}")
        elif manager == "radarr":
            radarr_client = RadarrClient()
            success = radarr_client.unmonitor_movie(manager_entity_id)
            lines.append(f"unmonitor={'ok' if success else 'failed'}")

    return {
        "handled": True,
        "result": "imported",
        "download_id": updated.get("id"),
        "download": updated.get("filename") or updated.get("id"),
        "lines": lines,
    }


def handle_add_webhook(normalized: dict) -> dict:
    manager = str(normalized.get("manager") or "")
    manager_entity_id = normalized.get("manager_entity_id")
    entity = normalized.get("entity") or {}
    entity_title = str(entity.get("title") or manager_entity_id or manager or "item")
    if manager not in {"sonarr", "radarr"} or manager_entity_id is None:
        return {"handled": False, "result": "unsupported"}

    manager_entity_id = int(manager_entity_id)
    if manager == "sonarr":
        show_id = sync_single_show(manager_entity_id, targeted=False)
        if not show_id:
            return {
                "handled": True,
                "manager": manager,
                "kind": "show",
                "result": "sync_failed",
                "title": entity_title,
            }
        result = automap_show(int(show_id), force=False, emit_logs=False)
        return {
            "handled": True,
            "manager": manager,
            "kind": "show",
            "item_id": int(show_id),
            "title": entity_title,
            "result": result,
        }

    movie_id = sync_single_movie(manager_entity_id, targeted=False)
    if not movie_id:
        return {
            "handled": True,
            "manager": manager,
            "kind": "movie",
            "result": "sync_failed",
            "title": entity_title,
        }
    result = automap_movie(int(movie_id), force=False, emit_logs=False)
    return {
        "handled": True,
        "manager": manager,
        "kind": "movie",
        "item_id": int(movie_id),
        "title": entity_title,
        "result": result,
    }


def handle_delete_webhook(normalized: dict) -> dict:
    manager = str(normalized.get("manager") or "")
    manager_entity_id = normalized.get("manager_entity_id")
    entity = normalized.get("entity") or {}
    local_match = normalized.get("local_match") or {}
    entity_title = str(entity.get("title") or local_match.get("title") or manager_entity_id or manager or "item")
    if manager not in {"sonarr", "radarr"} or manager_entity_id is None:
        return {"handled": False, "result": "unsupported"}

    manager_entity_id = int(manager_entity_id)
    if manager == "sonarr":
        removed = delete_show_by_sonarr_id(manager_entity_id)
        return {
            "handled": True,
            "manager": manager,
            "kind": "show",
            "item_id": local_match.get("id"),
            "title": entity_title,
            "result": "deleted" if removed else "not_found",
            "removed": int(removed),
        }

    removed = delete_movie_by_radarr_id(manager_entity_id)
    return {
        "handled": True,
        "manager": manager,
        "kind": "movie",
        "item_id": local_match.get("id"),
        "title": entity_title,
        "result": "deleted" if removed else "not_found",
        "removed": int(removed),
    }
