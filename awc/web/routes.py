"""Minimal route surface for the clean rebuild."""

import logging
import os
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from ..core.config import settings
from ..core.log_events import (
    format_movie_automap_lines,
    format_show_automap_lines,
    log_block,
    log_debug,
    log_exception,
    log_info,
    log_warning,
)
from ..core.logging import get_logger
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
)
from ..services.dashboard_service import build_dashboard_html, build_dashboard_snapshot, build_heartbeat_snapshot
from ..services.download_service import (
    build_download_snapshot,
    cancel_download,
    clear_download_history,
    create_fake_torrent,
    remove_download,
    resolve_legacy_download_request,
    resume_download,
)
from ..services.discovery_service import discover_movie, discover_show, search_animeworld
from ..services.events_service import stream_events
from ..services.health_service import build_health_report
from ..services.log_service import build_log_snapshot
from ..services.sanitizer_service import sanitizer_status, start_link_sanitizer
from ..services.mutation_service import (
    ignore_show_season,
    ignore_movie,
    map_movie,
    map_show_season,
    remove_movie,
    remove_show,
    unmap_all_mappings,
    unmap_movie,
    unmap_show_season,
)
from ..services.rss_service import build_rss_snapshot, clear_rss_cache
from ..services.torznab_service import build_caps_xml, build_search_xml
from ..services.webhook_service import normalize_webhook
from ..services.sync_runner_service import sync_all, sync_now_radarr, sync_now_sonarr, sync_single_movie, sync_single_show, sync_status
from .auth import require_api_key

api_router = APIRouter()
logger = get_logger("routes")


def _webhook_log_level(status: str) -> int:
    return logging.INFO if status in {"success", "already_mapped"} else logging.WARNING


def _log_webhook_show_result(title: str, show_id: int, result: dict) -> None:
    status = str(result.get("status") or "")
    if status in {"success", "partial", "ambiguous"}:
        show = build_show_snapshot(show_id) or {}
        lines = format_show_automap_lines(show, result.get("mapped_seasons", []), result.get("ambiguous", []))
    elif status == "already_mapped":
        lines = ["already mapped"]
    elif status == "error":
        lines = [str(result.get("message") or "error")]
    else:
        lines = ["No high-confidence AnimeWorld match found"]
    log_block(
        logger,
        _webhook_log_level(status),
        f"Webhook Sonarr add: {title}",
        lines,
        event_type="webhook.sonarr.add",
        entity_kind="show",
        entity_id=show_id,
        entity_title=title,
        details={"status": status},
    )


def _log_webhook_movie_result(title: str, movie_id: int, result: dict) -> None:
    status = str(result.get("status") or "")
    if status == "success":
        movie = build_movie_snapshot(movie_id) or {}
        lines = format_movie_automap_lines(movie.get("mapping"))
    elif status == "already_mapped":
        lines = ["already mapped"]
    elif status == "error":
        lines = [str(result.get("message") or "error")]
    else:
        lines = ["No high-confidence AnimeWorld match found"]
    log_block(
        logger,
        _webhook_log_level(status),
        f"Webhook Radarr add: {title}",
        lines,
        event_type="webhook.radarr.add",
        entity_kind="movie",
        entity_id=movie_id,
        entity_title=title,
        details={"status": status},
    )


def _map_show_season_impl(
    *,
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
) -> dict:
    show = build_show_snapshot(show_id)
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
    mapped_link = result.get("mapping", {}).get("aw_link", aw_link)
    log_block(
        logger,
        logging.INFO,
        str((show or {}).get("title") or f"show:{show_id}"),
        [f"S{season_number:02d} manual → {mapped_link}"],
    )
    return result


def _unmap_show_season_impl(*, show_id: int, season_number: int) -> dict:
    show = build_show_snapshot(show_id)
    result = unmap_show_season(show_id, season_number)
    log_block(
        logger,
        logging.INFO,
        str((show or {}).get("title") or f"show:{show_id}"),
        [f"S{season_number:02d} removed"],
    )
    return result


def _ignore_show_season_impl(*, show_id: int, season_number: int, ignored: bool) -> dict:
    show = build_show_snapshot(show_id)
    result = ignore_show_season(show_id, season_number, ignored)
    if not result["updated"]:
        raise HTTPException(status_code=404, detail="Season not found")
    log_block(
        logger,
        logging.INFO,
        str((show or {}).get("title") or f"show:{show_id}"),
        [f"S{season_number:02d} {'ignored' if ignored else 'unignored'}"],
    )
    return result


def _map_movie_impl(
    *,
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
) -> dict:
    movie = build_movie_snapshot(movie_id)
    result = map_movie(
        movie_id=movie_id,
        aw_link=aw_link,
        aw_title=aw_title,
        aw_status=aw_status,
        aw_category=aw_category,
    )
    if not result["updated"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    mapped_link = result.get("mapping", {}).get("aw_link", aw_link)
    log_block(
        logger,
        logging.INFO,
        str((movie or {}).get("title") or f"movie:{movie_id}"),
        [f"manual → {mapped_link}"],
    )
    return result


def _unmap_movie_impl(*, movie_id: int) -> dict:
    movie = build_movie_snapshot(movie_id)
    result = unmap_movie(movie_id)
    log_block(
        logger,
        logging.INFO,
        str((movie or {}).get("title") or f"movie:{movie_id}"),
        ["removed"],
    )
    return result


def _ignore_movie_impl(*, movie_id: int, ignored: bool) -> dict:
    result = ignore_movie(movie_id, ignored)
    if not result["updated"]:
        raise HTTPException(status_code=404, detail="Movie not found")
    movie = build_movie_snapshot(movie_id)
    log_block(
        logger,
        logging.INFO,
        str((movie or {}).get("title") or f"movie:{movie_id}"),
        ["ignored" if ignored else "unignored"],
    )
    return result


@api_router.get("/", tags=["UI"], summary="Dashboard", description="Serve the main AnimeWorld Companion web UI.", include_in_schema=False)
def dashboard() -> HTMLResponse:
    return HTMLResponse(build_dashboard_html())


@api_router.post(
    "/api/shows/{show_id}/seasons/{season_number}/map",
    tags=["Mutation"],
    summary="Map show season",
    description="Create or replace a manual AnimeWorld mapping for a Sonarr show season.",
)
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
    return _map_show_season_impl(
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


@api_router.post(
    "/api/shows/{show_id}/seasons/{season_number}/unmap",
    tags=["Mutation"],
    summary="Unmap show season",
    description="Remove all mappings for a specific Sonarr show season.",
)
def api_unmap_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    return _unmap_show_season_impl(show_id=show_id, season_number=season_number)


@api_router.post(
    "/api/shows/{show_id}/seasons/{season_number}/ignore",
    tags=["Mutation"],
    summary="Ignore show season",
    description="Mark a show season as ignored so it is excluded from normal mapping workflows.",
)
def api_ignore_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    return _ignore_show_season_impl(show_id=show_id, season_number=season_number, ignored=True)


@api_router.post(
    "/api/shows/{show_id}/seasons/{season_number}/unignore",
    tags=["Mutation"],
    summary="Unignore show season",
    description="Restore a previously ignored show season to normal mapping workflows.",
)
def api_unignore_show_season(
    show_id: int,
    season_number: int,
    _: str = Depends(require_api_key),
) -> dict:
    return _ignore_show_season_impl(show_id=show_id, season_number=season_number, ignored=False)


@api_router.post(
    "/api/movies/{movie_id}/ignore",
    tags=["Mutation"],
    summary="Ignore movie",
    description="Mark a movie as ignored so it is excluded from normal mapping workflows.",
)
def api_ignore_movie(
    movie_id: int,
    _: str = Depends(require_api_key),
) -> dict:
    return _ignore_movie_impl(movie_id=movie_id, ignored=True)


@api_router.post(
    "/api/movies/{movie_id}/unignore",
    tags=["Mutation"],
    summary="Unignore movie",
    description="Restore a previously ignored movie to normal mapping workflows.",
)
def api_unignore_movie(
    movie_id: int,
    _: str = Depends(require_api_key),
) -> dict:
    return _ignore_movie_impl(movie_id=movie_id, ignored=False)


@api_router.delete("/api/shows/{show_id}", tags=["Mutation"], summary="Delete show", description="Remove a synced show and all related local AWC state.")
def api_delete_show(show_id: int, _: str = Depends(require_api_key)) -> dict:
    show = build_show_snapshot(show_id)
    result = remove_show(show_id)
    if not result["removed"]:
        raise HTTPException(status_code=404, detail="Show not found")
    log_block(
        logger,
        logging.INFO,
        str((show or {}).get("title") or f"show:{show_id}"),
        ["deleted"],
    )
    return result


@api_router.delete("/api/movies/{movie_id}", tags=["Mutation"], summary="Delete movie", description="Remove a synced movie and all related local AWC state.")
def api_delete_movie(movie_id: int, _: str = Depends(require_api_key)) -> dict:
    movie = build_movie_snapshot(movie_id)
    result = remove_movie(movie_id)
    if not result["removed"]:
        raise HTTPException(status_code=404, detail="Movie not found")
    log_block(
        logger,
        logging.INFO,
        str((movie or {}).get("title") or f"movie:{movie_id}"),
        ["deleted"],
    )
    return result


@api_router.post(
    "/api/automap",
    tags=["Automap"],
    summary="Run automap",
    description="Run automap for the whole library, one show, one movie, or one show season depending on `kind`, `item_id`, and `season_number`.",
)
def api_automap(
    kind: str | None = Query(default=None),
    item_id: int | None = Query(default=None),
    season_number: int | None = Query(default=None),
    force: bool = False,
    _: str = Depends(require_api_key),
) -> dict:
    normalized_kind = (kind or "").strip().lower()
    if not normalized_kind and item_id is None and season_number is None:
        return start_automap_all(force=force)
    if normalized_kind not in {"show", "movie"} or item_id is None:
        raise HTTPException(status_code=422, detail="Use /automap with kind=show|movie and item_id=...")
    if normalized_kind == "movie":
        if season_number is not None:
            raise HTTPException(status_code=422, detail="Movies do not support season_number")
        return automap_movie(item_id, force=force)
    return automap_show(item_id, season_number=season_number, force=force)


@api_router.post(
    "/api/movies/{movie_id}/map",
    tags=["Mutation"],
    summary="Map movie",
    description="Create or replace a manual AnimeWorld mapping for a Radarr movie.",
)
def api_map_movie(
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
    _: str = Depends(require_api_key),
) -> dict:
    return _map_movie_impl(
        movie_id=movie_id,
        aw_link=aw_link,
        aw_title=aw_title,
        aw_status=aw_status,
        aw_category=aw_category,
    )


@api_router.post(
    "/api/movies/{movie_id}/unmap",
    tags=["Mutation"],
    summary="Unmap movie",
    description="Remove the current AnimeWorld mapping for a Radarr movie.",
)
def api_unmap_movie(movie_id: int, _: str = Depends(require_api_key)) -> dict:
    return _unmap_movie_impl(movie_id=movie_id)


@api_router.get("/api/status", tags=["System"], summary="Runtime status", description="Return high-level runtime, sync, automap, and sanitizer status.")
def api_status(_: str = Depends(require_api_key)) -> dict:
    return {
        "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
        "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
        "sync": sync_status(),
        "runtime": runtime_state(),
        "automap": automap_status(),
        "links": sanitizer_status(),
    }


@api_router.get("/api/system/health", tags=["System"], summary="Detailed health", description="Return the structured internal health report used by the dashboard and diagnostics.")
def api_system_health(_: str = Depends(require_api_key)) -> dict:
    return build_health_report()


@api_router.get("/api/heartbeat", tags=["System"], summary="Heartbeat snapshot", description="Return lightweight live dashboard heartbeat data.")
def api_heartbeat(_: str = Depends(require_api_key)) -> dict:
    return build_heartbeat_snapshot()


@api_router.post(
    "/api/mappings/unmap-all",
    tags=["Mutation"],
    summary="Unmap all mappings",
    description="Remove all stored AnimeWorld mappings for shows, movies, or both.",
)
def api_unmap_all_mappings(
    kind: str = Query(default="all"),
    _: str = Depends(require_api_key),
) -> dict:
    result = unmap_all_mappings(kind=kind)
    if not result.get("updated"):
        raise HTTPException(status_code=422, detail=result.get("reason") or "invalid request")
    log_info(
        logger,
        "mutation.unmap_all",
        f"Bulk unmap: cleared {result.get('shows', {}).get('shows', 0)} shows and {result.get('movies', {}).get('movies', 0)} movies.",
        details=result,
    )
    return result


@api_router.get("/api/shows", tags=["Catalog"], summary="List shows", description="List synced Sonarr shows currently present in the AWC catalog.")
def api_shows(limit: int = 100, _: str = Depends(require_api_key)) -> list[dict]:
    return build_catalog_snapshot(show_limit=limit, movie_limit=0)["shows"]


@api_router.get("/api/shows/{show_id}", tags=["Catalog"], summary="Get show", description="Return one synced show with seasons, mappings, and alternate titles.")
def api_show_detail(show_id: int, _: str = Depends(require_api_key)) -> dict:
    show = build_show_snapshot(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    return show


@api_router.get("/api/movies", tags=["Catalog"], summary="List movies", description="List synced Radarr movies currently present in the AWC catalog.")
def api_movies(limit: int = 100, _: str = Depends(require_api_key)) -> list[dict]:
    return build_catalog_snapshot(show_limit=0, movie_limit=limit)["movies"]

@api_router.get("/api/movies/{movie_id}", tags=["Catalog"], summary="Get movie", description="Return one synced movie with mapping and alternate titles.")
def api_movie_detail(movie_id: int, _: str = Depends(require_api_key)) -> dict:
    movie = build_movie_snapshot(movie_id)
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie


@api_router.get("/api/search/aw", tags=["Discovery"], summary="Search AnimeWorld", description="Run a raw AnimeWorld search and return merged API + scrape candidate links.")
def api_search_aw(q: str, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    return search_animeworld(q, limit=limit)


@api_router.get("/api/discover/{show_id}", tags=["Discovery"], summary="Discover show candidates", description="Discover raw AnimeWorld candidates and metadata for one synced show.")
def api_discover_show(show_id: int, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    result = discover_show(show_id, limit=limit)
    if not result:
        raise HTTPException(status_code=404, detail="Show not found")
    return result


@api_router.get("/api/discover/movie/{movie_id}", tags=["Discovery"], summary="Discover movie candidates", description="Discover raw AnimeWorld candidates and metadata for one synced movie.")
def api_discover_movie(movie_id: int, limit: int = 10, _: str = Depends(require_api_key)) -> dict:
    result = discover_movie(movie_id, limit=limit)
    if not result:
        raise HTTPException(status_code=404, detail="Movie not found")
    return result


@api_router.get("/api/rss/cache", tags=["System"], summary="RSS cache snapshot", description="Inspect the current cached RSS items that AWC may republish to Arr clients.")
def api_rss_cache(limit: int = 100, _: str = Depends(require_api_key)) -> dict:
    return build_rss_snapshot(limit=limit)


@api_router.get(
    "/download",
    tags=["Download"],
    summary="Torrent handoff",
    description="Return the fake torrent handoff used by Sonarr and Radarr. Supports both the current internal parameters and the old `url/save_name/aw_link` contract.",
)
def download_handoff(
    request: Request,
    manager: str = "",
    title: str = "",
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    manager_id: int | None = None,
    source: str = "",
    release_source: str = "unknown",
    url: str = "",
    save_name: str = "",
    aw_link: str = "",
    _: str = Depends(require_api_key),
) -> Response:
    if not manager or not title:
        resolved = resolve_legacy_download_request(url=url, save_name=save_name, aw_link=aw_link)
        if not resolved:
            raise HTTPException(status_code=422, detail="Missing download context")
        manager = str(resolved.get("manager") or manager)
        title = str(resolved.get("title") or title)
        season = resolved.get("season_number", season)
        episode = resolved.get("episode_number", episode)
        year = resolved.get("year", year)
        manager_id = resolved.get("manager_id", manager_id)
        source = str(resolved.get("source") or url or source)
        save_name = str(resolved.get("filename") or save_name)
        aw_link = str(resolved.get("aw_link") or aw_link)
    download, torrent_bytes, torrent_name = create_fake_torrent(
        manager=manager,
        title=title,
        season=season,
        episode=episode,
        year=year,
        source=source or url,
        manager_id=manager_id,
        aw_link=aw_link,
        filename=save_name or None,
        release_source=release_source,
        base_url=str(request.base_url).rstrip("/"),
    )
    if download is None:
        raise HTTPException(status_code=422, detail="No source URL available for this download")
    headers = {
        "Content-Disposition": f'attachment; filename="{torrent_name}"',
        "X-AWC-Download-Id": str(download.get("id", "")),
    }
    return Response(content=torrent_bytes, media_type="application/x-bittorrent", headers=headers)


@api_router.get("/api/downloads", tags=["Download"], summary="List downloads", description="Return tracked AWC download jobs with progress, status, and summary counts.")
def api_downloads(limit: int = 100, _: str = Depends(require_api_key)) -> dict:
    return build_download_snapshot(limit=limit)


@api_router.post("/api/downloads/{download_id}/cancel", tags=["Download"], summary="Pause download", description="Pause an active download and keep its partial file for later resume.")
def api_cancel_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    download = cancel_download(download_id)
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@api_router.post("/api/downloads/{download_id}/resume", tags=["Download"], summary="Resume download", description="Resume a paused download from its current partial file.")
def api_resume_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    download = resume_download(download_id)
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    return download


@api_router.post("/api/downloads/{download_id}/remove", tags=["Download"], summary="Remove download", description="Remove a tracked download row and its local partial/final file when applicable.")
def api_remove_download(download_id: str, _: str = Depends(require_api_key)) -> dict:
    removed = remove_download(download_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Download not found")
    return {"removed": True, "download_id": download_id}


@api_router.post("/api/downloads/clear", tags=["Download"], summary="Clear finished downloads", description="Remove finished or terminal download history entries from AWC.")
def api_clear_downloads(_: str = Depends(require_api_key)) -> dict:
    return {"removed": clear_download_history()}


@api_router.get("/api/events", tags=["System"], summary="Server-sent events", description="Stream dashboard events and runtime updates over SSE.")
def api_events(_: str = Depends(require_api_key)) -> StreamingResponse:
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@api_router.post("/api/rss/cache/clear", tags=["System"], summary="Clear RSS cache", description="Delete cached RSS entries currently stored by AWC.")
def api_clear_rss_cache(_: str = Depends(require_api_key)) -> dict:
    result = clear_rss_cache()
    log_info(
        logger,
        "rss.cache.cleared",
        "RSS cache cleared",
        lines=[f"removed={result.get('removed', 0)}"],
        details=result,
    )
    return result


@api_router.get(
    "/api/logs",
    tags=["Logs"],
    summary="Query persistent logs",
    description="Query the structured log history stored in logging.db.",
)
def api_logs(
    level: str = Query(default=""),
    logger_name: str = Query(default=""),
    event_type: str = Query(default=""),
    since: str = Query(default=""),
    until: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
    _: str = Depends(require_api_key),
) -> dict:
    return build_log_snapshot(
        level=level,
        logger_name=logger_name,
        event_type=event_type,
        since=since,
        until=until,
        q=q,
        limit=limit,
    )


@api_router.post("/api/rss/update", tags=["System"], summary="Update RSS cache", description="Fetch the AnimeWorld RSS feed now and republish any matching items into the local cache.")
def api_update_rss_cache(_: str = Depends(require_api_key)) -> dict:
    result = update_rss_cache()
    log_info(
        logger,
        "rss.cache.update",
        "RSS cache update requested",
        lines=[f"cached={result.get('cached', 0)}"],
        details=result,
    )
    return result


@api_router.post("/api/links/sanitize", tags=["System"], summary="Run sanitizer", description="Trigger a background sanitizer run to verify mappings, refresh metadata, and follow live redirects.")
def api_sanitize_links(_: str = Depends(require_api_key)) -> dict:
    result = start_link_sanitizer()
    log_info(logger, "sanitizer.request.api", "Sanitizer requested")
    return result


@api_router.post("/api/system/restart", tags=["System"], summary="Restart container", description="Gracefully stop the app process so Docker can restart the container.")
def api_restart(_: str = Depends(require_api_key)) -> dict:
    def _graceful_exit() -> None:
        from ..services.events_service import stop_sse_streams

        stop_sse_streams()
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=_graceful_exit, name="awc-restart", daemon=True).start()
    log_warning(logger, "system.restart", "Restart requested")
    return {"ok": True, "message": "Restart scheduled"}


@api_router.post("/api/webhook", tags=["Integration"], summary="Manager webhook", description="Accept Sonarr/Radarr webhook payloads and trigger the matching sync and automap flows.")
def manager_webhook(
    payload: dict = Body(default_factory=dict),
    _: str = Depends(require_api_key),
) -> dict:
    normalized = normalize_webhook(payload)
    if not normalized["accepted"]:
        raise HTTPException(status_code=400, detail="Unsupported webhook payload")
    if normalized["event_family"] == "add" and normalized.get("manager_entity_id"):
        manager = str(normalized.get("manager") or "")
        manager_entity_id = int(normalized["manager_entity_id"])
        entity_title = str((normalized.get("entity") or {}).get("title") or manager_entity_id)

        def _run() -> None:
            try:
                if manager == "sonarr":
                    show_id = sync_single_show(manager_entity_id, targeted=False)
                    if show_id:
                        result = automap_show(int(show_id), force=False, emit_logs=False)
                        _log_webhook_show_result(entity_title, int(show_id), result)
                    else:
                        log_block(
                            logger,
                            logging.WARNING,
                            f"Webhook Sonarr add: {entity_title}",
                            ["sync failed"],
                            event_type="webhook.sonarr.add",
                            entity_kind="show",
                            entity_id=manager_entity_id,
                            entity_title=entity_title,
                            details={"status": "sync_failed"},
                        )
                elif manager == "radarr":
                    movie_id = sync_single_movie(manager_entity_id, targeted=False)
                    if movie_id:
                        result = automap_movie(int(movie_id), force=False, emit_logs=False)
                        _log_webhook_movie_result(entity_title, int(movie_id), result)
                    else:
                        log_block(
                            logger,
                            logging.WARNING,
                            f"Webhook Radarr add: {entity_title}",
                            ["sync failed"],
                            event_type="webhook.radarr.add",
                            entity_kind="movie",
                            entity_id=manager_entity_id,
                            entity_title=entity_title,
                            details={"status": "sync_failed"},
                        )
            except Exception:
                log_exception(
                    logger,
                    "webhook.add.failed",
                    "Webhook add handler failed",
                    details={"manager": manager, "manager_id": manager_entity_id},
                    entity_kind=manager or None,
                    entity_id=manager_entity_id,
                    entity_title=entity_title,
                )

        threading.Thread(target=_run, name=f"awc-webhook-{manager or 'manager'}", daemon=True).start()
    return normalized


@api_router.post("/api/sync", tags=["Integration"], summary="Sync all managers", description="Run a full Sonarr + Radarr sync immediately.")
def manual_sync(_: str = Depends(require_api_key)) -> dict:
    result = sync_all()
    log_info(
        logger,
        "sync.manual.all",
        "Manual sync completed",
        lines=[f"sonarr={result.get('sonarr', 0)}", f"radarr={result.get('radarr', 0)}"],
        details=result,
    )
    return {"status": "ok", "result": result}


@api_router.post("/api/sync/sonarr", tags=["Integration"], summary="Sync Sonarr", description="Run a Sonarr-only sync immediately.")
def manual_sync_sonarr(_: str = Depends(require_api_key)) -> dict:
    result = sync_now_sonarr()
    log_info(
        logger,
        "sync.manual.sonarr",
        "Manual Sonarr sync completed",
        lines=[f"sonarr={result.get('sonarr', 0)}"],
        details=result,
    )
    return {"status": "ok", "result": result}


@api_router.post("/api/sync/radarr", tags=["Integration"], summary="Sync Radarr", description="Run a Radarr-only sync immediately.")
def manual_sync_radarr(_: str = Depends(require_api_key)) -> dict:
    result = sync_now_radarr()
    log_info(
        logger,
        "sync.manual.radarr",
        "Manual Radarr sync completed",
        lines=[f"radarr={result.get('radarr', 0)}"],
        details=result,
    )
    return {"status": "ok", "result": result}


def _torznab_response(
    request: Request,
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
    base_url = str(request.base_url).rstrip("/")
    log_debug(
        logger,
        "torznab.request",
        "Torznab request",
        details={
            "type": request_type,
            "q": q,
            "season": season,
            "episode": ep,
            "category": cat,
            "tvdb_id": tvdbid,
            "tmdb_id": tmdbid,
            "imdb_id": imdbid,
        },
    )
    if request_type == "caps":
        return Response(content=build_caps_xml(base_url=base_url), media_type="application/xml")
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
                base_url=base_url,
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
                base_url=base_url,
            ),
            media_type="application/xml",
        )
    raise HTTPException(status_code=400, detail=f"Unsupported Torznab operation: {t}")


@api_router.get(
    "/api",
    tags=["Indexer"],
    summary="Torznab endpoint",
    description="Torznab-compatible indexer endpoint used by Sonarr and Radarr for caps, search, tvsearch, movie search, and cached RSS.",
)
def torznab_api(
    request: Request,
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
    return _torznab_response(request, t=t, q=q, season=season, ep=ep, cat=cat, imdbid=imdbid, tmdbid=tmdbid, tvdbid=tvdbid)


@api_router.post("/api", include_in_schema=False)
def torznab_api_post(
    request: Request,
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
    return _torznab_response(request, t=t, q=q, season=season, ep=ep, cat=cat, imdbid=imdbid, tmdbid=tmdbid, tvdbid=tvdbid)
