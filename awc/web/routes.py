"""Minimal route surface for the clean rebuild."""

import os
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from ..core.config import settings
from ..services.catalog_service import (
    build_catalog_snapshot,
    build_movie_snapshot,
    build_show_snapshot,
)
from ..services.background_service import runtime_state, update_rss_cache
from ..services.automap_service import (
    automap_movie,
    automap_show,
    automap_status,
    start_automap_all,
    start_automap_all_movies,
)
from ..services.dashboard_service import build_dashboard_html, build_dashboard_snapshot
from ..services.download_service import (
    build_download_snapshot,
    cancel_download,
    clear_download_history,
    create_fake_torrent,
    remove_download,
    resume_download,
)
from ..services.discovery_service import discover_movie, discover_show, search_animeworld
from ..services.events_service import stream_events
from ..services.health_service import build_health_report
from ..services.link_sanitizer_service import sanitizer_status, start_link_sanitizer
from ..services.mapping_service import (
    build_show_mapping_snapshot,
    resolve_absolute_episode,
    resolve_scene_episode,
)
from ..services.mutation_service import (
    ignore_show_season,
    map_movie,
    map_show_season,
    remove_show,
    unmap_movie,
    unmap_show_season,
)
from ..services.manager_service import build_manager_snapshot
from ..services.preview_service import build_naming_preview
from ..services.query_service import parse_query, resolve_local_query
from ..services.rss_service import build_rss_snapshot, clear_rss_cache
from ..services.sync_service import build_sync_overview
from ..services.torznab_service import build_caps_xml, build_search_xml
from ..services.webhook_service import normalize_webhook
from ..services.sync_runner_service import sync_all, sync_single_movie, sync_single_show, sync_status
from .auth import require_api_key

api_router = APIRouter()


@api_router.get("/", tags=["UI"])
def dashboard() -> HTMLResponse:
    return HTMLResponse(build_dashboard_html())


@api_router.post("/map", tags=["Mutation"])
def api_map_show_season(
    show_id: int,
    season_number: int,
    aw_link: str,
    aw_title: str = "",
    part: int = 1,
    aw_episode_count: int = 0,
    aw_total_episodes: int = 0,
    aw_status: str = "",
    aw_category: str = "",
    linked_with_season: int | None = None,
    _: str = Depends(require_api_key),
) -> dict:
    result = map_show_season(
        show_id=show_id,
        season_number=season_number,
        aw_link=aw_link,
        aw_title=aw_title,
        part=part,
        aw_episode_count=aw_episode_count,
        aw_total_episodes=aw_total_episodes,
        aw_status=aw_status,
        aw_category=aw_category,
        linked_with_season=linked_with_season,
    )
    if not result["updated"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@api_router.post("/unmap", tags=["Mutation"])
def api_unmap_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    return unmap_show_season(show_id, season_number)


@api_router.post("/ignore-season", tags=["Mutation"])
def api_ignore_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    result = ignore_show_season(show_id, season_number, True)
    if not result["updated"]:
        raise HTTPException(status_code=404, detail="Season not found")
    return result


@api_router.post("/unignore-season", tags=["Mutation"])
def api_unignore_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    result = ignore_show_season(show_id, season_number, False)
    if not result["updated"]:
        raise HTTPException(status_code=404, detail="Season not found")
    return result


@api_router.post("/delete-show", tags=["Mutation"])
def api_delete_show(show_id: int, _: str = Depends(require_api_key)) -> dict:
    result = remove_show(show_id)
    if not result["removed"]:
        raise HTTPException(status_code=404, detail="Show not found")
    return result


@api_router.post("/automap", tags=["Automap"])
def api_automap_all(force: bool = False, _: str = Depends(require_api_key)) -> dict:
    return start_automap_all(force=force)


@api_router.post("/automap/movies", tags=["Automap"])
def api_automap_all_movies(force: bool = False, _: str = Depends(require_api_key)) -> dict:
    return start_automap_all_movies(force=force)


@api_router.post("/automap/movie/{movie_id}", tags=["Automap"])
def api_automap_movie(movie_id: int, force: bool = False, _: str = Depends(require_api_key)) -> dict:
    return automap_movie(movie_id, force=force)


@api_router.post("/automap/{show_id}", tags=["Automap"])
def api_automap_show(show_id: int, force: bool = False, _: str = Depends(require_api_key)) -> dict:
    return automap_show(show_id, force=force)


@api_router.post("/automap/{show_id}/{season_number}", tags=["Automap"])
def api_automap_show_season(
    show_id: int,
    season_number: int,
    force: bool = False,
    _: str = Depends(require_api_key),
) -> dict:
    return automap_show(show_id, season_number=season_number, force=force)


@api_router.post("/map/movie/{movie_id}", tags=["Mutation"])
def api_map_movie(
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
    _: str = Depends(require_api_key),
) -> dict:
    result = map_movie(
        movie_id=movie_id,
        aw_link=aw_link,
        aw_title=aw_title,
        aw_status=aw_status,
        aw_category=aw_category,
    )
    if not result["updated"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


@api_router.post("/unmap/movie/{movie_id}", tags=["Mutation"])
def api_unmap_movie(movie_id: int, _: str = Depends(require_api_key)) -> dict:
    return unmap_movie(movie_id)


@api_router.get("/api/rebuild/status", tags=["System"])
def rebuild_status() -> dict:
    return {
        "phase": "foundation",
        "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
        "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
        "sync": sync_status(),
        "runtime": runtime_state(),
        "automap": automap_status(),
        "links": sanitizer_status(),
    }


@api_router.get("/api/rebuild/health", tags=["System"])
def rebuild_health() -> dict:
    return build_health_report()


@api_router.get("/api/heartbeat", tags=["System"])
def api_heartbeat() -> dict:
    return build_dashboard_snapshot()


@api_router.get("/api/rebuild/managers", tags=["System"])
def rebuild_managers() -> dict:
    return build_manager_snapshot()


@api_router.get("/api/rebuild/sync-overview", tags=["System"])
def rebuild_sync_overview() -> dict:
    return build_sync_overview()


@api_router.get("/api/rebuild/catalog", tags=["System"])
def rebuild_catalog(show_limit: int = 10, movie_limit: int = 10) -> dict:
    return build_catalog_snapshot(show_limit=show_limit, movie_limit=movie_limit)


@api_router.get("/api/shows", tags=["Catalog"])
def api_shows(limit: int = 100, _: str = Depends(require_api_key)) -> list[dict]:
    return build_catalog_snapshot(show_limit=limit, movie_limit=0)["shows"]


@api_router.get("/api/rebuild/shows/{show_id}", tags=["System"])
def rebuild_show_detail(show_id: int) -> dict:
    show = build_show_snapshot(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    return show


@api_router.get("/api/shows/{show_id}", tags=["Catalog"])
def api_show_detail(show_id: int, _: str = Depends(require_api_key)) -> dict:
    return rebuild_show_detail(show_id)


@api_router.get("/api/movies", tags=["Catalog"])
def api_movies(limit: int = 100, _: str = Depends(require_api_key)) -> list[dict]:
    return build_catalog_snapshot(show_limit=0, movie_limit=limit)["movies"]


@api_router.get("/api/rebuild/movies/{movie_id}", tags=["System"])
def rebuild_movie_detail(movie_id: int) -> dict:
    movie = build_movie_snapshot(movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie


@api_router.get("/api/movies/{movie_id}", tags=["Catalog"])
def api_movie_detail(movie_id: int, _: str = Depends(require_api_key)) -> dict:
    return rebuild_movie_detail(movie_id)


@api_router.get("/api/search/aw", tags=["Discovery"])
def api_search_aw(q: str, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    return search_animeworld(q, limit=limit)


@api_router.get("/api/discover/{show_id}", tags=["Discovery"])
def api_discover_show(show_id: int, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    result = discover_show(show_id, limit=limit)
    if not result:
        raise HTTPException(status_code=404, detail="Show not found")
    return result


@api_router.get("/api/discover/movie/{movie_id}", tags=["Discovery"])
def api_discover_movie(movie_id: int, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    result = discover_movie(movie_id, limit=limit)
    if not result:
        raise HTTPException(status_code=404, detail="Movie not found")
    return result


@api_router.get("/api/rss/cache", tags=["System"])
def api_rss_cache(limit: int = 100, _: str = Depends(require_api_key)) -> dict:
    return build_rss_snapshot(limit=limit)


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


@api_router.get("/download", tags=["Download"])
def download_handoff(
    manager: str,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    manager_id: int | None = None,
    source: str = "",
    _: str = Depends(require_api_key),
) -> Response:
    download, torrent_bytes, torrent_name = create_fake_torrent(
        manager=manager,
        title=title,
        season=season,
        episode=episode,
        year=year,
        source=source,
        manager_id=manager_id,
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{torrent_name}"',
        "X-AWC-Download-Id": str(download.get("id", "")),
    }
    return Response(content=torrent_bytes, media_type="application/x-bittorrent", headers=headers)


@api_router.get("/api/downloads", tags=["Download"])
def api_downloads(limit: int = 100, _: str = Depends(require_api_key)) -> dict:
    return build_download_snapshot(limit=limit)


@api_router.post("/api/downloads/{download_id}/cancel", tags=["Download"])
def api_cancel_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    download = cancel_download(download_id)
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@api_router.post("/api/downloads/{download_id}/resume", tags=["Download"])
def api_resume_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    download = resume_download(download_id)
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@api_router.post("/api/downloads/{download_id}/remove", tags=["Download"])
def api_remove_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    removed = remove_download(download_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Download not found")
    return {"removed": True, "download_id": download_id}


@api_router.post("/api/downloads/clear", tags=["Download"])
def api_clear_downloads(_: str = Depends(require_api_key)) -> dict:
    return {"removed": clear_download_history()}


@api_router.get("/api/events", tags=["System"])
def api_events(_: str = Depends(require_api_key)) -> StreamingResponse:
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@api_router.post("/api/rss/cache/clear", tags=["System"])
def api_clear_rss_cache(_: str = Depends(require_api_key)) -> dict:
    return clear_rss_cache()


@api_router.post("/api/rss/update", tags=["System"])
def api_update_rss_cache(_: str = Depends(require_api_key)) -> dict:
    return update_rss_cache()


@api_router.post("/api/links/sanitize", tags=["System"])
def api_sanitize_links(_: str = Depends(require_api_key)) -> dict:
    return start_link_sanitizer()


@api_router.post("/restart", tags=["System"])
def api_restart(_: str = Depends(require_api_key)) -> dict:
    def _graceful_exit() -> None:
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=_graceful_exit, name="awc-restart", daemon=True).start()
    return {"ok": True, "message": "Restart scheduled"}


@api_router.post("/api/webhook", tags=["Integration"])
def manager_webhook(
    payload: dict = Body(default_factory=dict),
    _: str = Depends(require_api_key),
) -> dict:
    normalized = normalize_webhook(payload)
    if not normalized["accepted"]:
        raise HTTPException(status_code=400, detail="Unsupported webhook payload")
    if normalized["event_family"] == "add" and normalized.get("manager_entity_id"):
        if normalized["manager"] == "sonarr":
            sync_single_show(int(normalized["manager_entity_id"]))
        elif normalized["manager"] == "radarr":
            sync_single_movie(int(normalized["manager_entity_id"]))
    return normalized


@api_router.post("/sync", tags=["Integration"])
def manual_sync(_: str = Depends(require_api_key)) -> dict:
    return {"status": "ok", "result": sync_all()}


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
