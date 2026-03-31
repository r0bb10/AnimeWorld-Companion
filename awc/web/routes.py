"""Minimal route surface for the clean rebuild."""

from fastapi import APIRouter, HTTPException

from ..core.config import settings
from ..services.catalog_service import (
    build_catalog_snapshot,
    build_movie_snapshot,
    build_show_snapshot,
)
from ..services.health_service import build_health_report
from ..services.mapping_service import (
    build_show_mapping_snapshot,
    resolve_absolute_episode,
    resolve_scene_episode,
)
from ..services.preview_service import build_naming_preview
from ..services.query_service import parse_query, resolve_local_query

api_router = APIRouter()


@api_router.get("/api/rebuild/status", tags=["System"])
def rebuild_status() -> dict:
    return {
        "phase": "foundation",
        "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
        "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
    }


@api_router.get("/api/rebuild/health", tags=["System"])
def rebuild_health() -> dict:
    return build_health_report()


@api_router.get("/api/rebuild/catalog", tags=["System"])
def rebuild_catalog(show_limit: int = 10, movie_limit: int = 10) -> dict:
    return build_catalog_snapshot(show_limit=show_limit, movie_limit=movie_limit)


@api_router.get("/api/rebuild/shows/{show_id}", tags=["System"])
def rebuild_show_detail(show_id: int) -> dict:
    show = build_show_snapshot(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    return show


@api_router.get("/api/rebuild/movies/{movie_id}", tags=["System"])
def rebuild_movie_detail(movie_id: int) -> dict:
    movie = build_movie_snapshot(movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie


@api_router.get("/api/rebuild/shows/{show_id}/mappings", tags=["System"])
def rebuild_show_mappings(show_id: int) -> dict:
    snapshot = build_show_mapping_snapshot(show_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Show not found")
    return snapshot


@api_router.get("/api/rebuild/shows/{show_id}/resolve-scene", tags=["System"])
def rebuild_resolve_scene(show_id: int, season: int, episode: int) -> dict:
    return resolve_scene_episode(show_id, season, episode)


@api_router.get("/api/rebuild/shows/{show_id}/resolve-absolute", tags=["System"])
def rebuild_resolve_absolute(show_id: int, absolute_episode: int) -> dict:
    return resolve_absolute_episode(show_id, absolute_episode)


@api_router.get("/api/rebuild/parse-query", tags=["System"])
def rebuild_parse_query(q: str) -> dict:
    return parse_query(q)


@api_router.get("/api/rebuild/resolve-query", tags=["System"])
def rebuild_resolve_query(q: str, media: str = "show") -> dict:
    return resolve_local_query(q, media=media)


@api_router.get("/api/rebuild/preview-name", tags=["System"])
def rebuild_preview_name(q: str, media: str = "show") -> dict:
    return build_naming_preview(q, media=media)
