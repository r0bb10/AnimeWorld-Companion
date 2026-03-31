"""Minimal route surface for the clean rebuild."""

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response

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
from ..services.manager_service import build_manager_snapshot
from ..services.preview_service import build_naming_preview
from ..services.query_service import parse_query, resolve_local_query
from ..services.sync_service import build_sync_overview
from ..services.torznab_service import build_caps_xml, build_search_xml
from ..services.webhook_service import normalize_webhook
from .auth import require_api_key

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


@api_router.get("/api/rebuild/managers", tags=["System"])
def rebuild_managers() -> dict:
    return build_manager_snapshot()


@api_router.get("/api/rebuild/sync-overview", tags=["System"])
def rebuild_sync_overview() -> dict:
    return build_sync_overview()


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


@api_router.get("/api/rebuild/placeholder/{item_id}", tags=["System"])
def rebuild_placeholder(item_id: str) -> dict:
    return {
        "item_id": item_id,
        "status": "placeholder",
        "detail": "Search contract is wired, download execution is not rebuilt yet.",
    }


@api_router.post("/api/webhook", tags=["Integration"])
def manager_webhook(
    payload: dict = Body(default_factory=dict),
    _: str = Depends(require_api_key),
) -> dict:
    normalized = normalize_webhook(payload)
    if not normalized["accepted"]:
        raise HTTPException(status_code=400, detail="Unsupported webhook payload")
    return normalized


@api_router.api_route("/api", methods=["GET", "POST"], tags=["Indexer"])
def torznab_api(
    _: str = Depends(require_api_key),
    t: str = Query(default="caps"),
    q: str = Query(default=""),
    season: int | None = Query(default=None),
    ep: int | None = Query(default=None),
    cat: str = Query(default=""),
    imdbid: str = Query(default=""),
    tmdbid: int | None = Query(default=None),
    tvdbid: int | None = Query(default=None),
) -> Response:
    request_type = (t or "caps").strip().lower()
    if request_type == "caps":
        return Response(content=build_caps_xml(), media_type="application/xml")
    if request_type in {"search", "tvsearch"}:
        return Response(
            content=build_search_xml(
                query=q,
                media="search",
                season=season,
                episode=ep,
                category=cat,
                tvdb_id=tvdbid,
                tmdb_id=tmdbid,
                imdb_id=imdbid,
            ),
            media_type="application/xml",
        )
    if request_type == "movie":
        return Response(
            content=build_search_xml(
                query=q,
                media="movie",
                category=cat,
                tmdb_id=tmdbid,
                imdb_id=imdbid,
            ),
            media_type="application/xml",
        )
    raise HTTPException(status_code=400, detail=f"Unsupported Torznab operation: {t}")
