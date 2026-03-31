"""Legacy-style dashboard rendering backed by the rebuilt core."""

from datetime import UTC, date, datetime
from pathlib import Path
import os
import time

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core.config import settings
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.sync_meta import get_sync_meta
from .automap_service import automap_status
from .background_service import runtime_state
from .catalog_service import build_catalog_snapshot, build_movie_snapshot, build_show_snapshot
from .download_service import build_download_snapshot
from .health_service import build_health_report
from .manager_service import build_manager_snapshot
from .rss_service import build_rss_snapshot
from .sync_runner_service import sync_status

_templates_dir = Path(__file__).resolve().parents[1] / "templates"
_app_start_time = time.time()
_jinja = Environment(
    loader=FileSystemLoader(str(_templates_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)


def season_has_aired(season: dict) -> bool:
    end_value = (season or {}).get("air_date_end") or (season or {}).get("air_date_start")
    if not end_value:
        return False
    try:
        return date.fromisoformat(str(end_value)[:10]) <= datetime.now(UTC).date()
    except ValueError:
        return False


def slug_to_url(slug: str) -> str:
    return AnimeWorldClient().slug_to_url(slug)


_jinja.globals["season_has_aired"] = season_has_aired
_jinja.globals["slug_to_url"] = slug_to_url


def _show_payload(show_id: int) -> dict | None:
    show = build_show_snapshot(show_id)
    if not show:
        return None
    mappings = {}
    for season in show.get("seasons", []):
        season_mappings = season.get("mappings", [])
        if season_mappings:
            mappings[season["season_number"]] = season_mappings
    return {
        **show,
        "alternate_titles": [item.get("title", "") for item in show.get("alternate_titles", []) if item.get("title")],
        "mappings": mappings,
    }


def _movie_payload(movie_id: int) -> dict | None:
    movie = build_movie_snapshot(movie_id)
    if not movie:
        return None
    return {
        **movie,
        "alternate_titles": [item.get("title", "") for item in movie.get("alternate_titles", []) if item.get("title")],
    }


def build_dashboard_snapshot() -> dict:
    health = build_health_report()
    managers = build_manager_snapshot()
    rss = build_rss_snapshot(limit=settings.rss_cache_limit)
    return {
        "catalog": build_catalog_snapshot(show_limit=250, movie_limit=250),
        "downloads": build_download_snapshot(limit=100),
        "runtime": {
            "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
            "animeworld_url": settings.aw_base_url,
            "last_sonarr_sync": get_sync_meta("last_sonarr_sync"),
            "last_radarr_sync": get_sync_meta("last_radarr_sync"),
        },
        "health": health,
        "managers": managers,
        "rss": rss,
        "sync": sync_status(),
        "automap": automap_status(),
    }


def build_heartbeat_snapshot() -> dict:
    health = build_health_report()
    manager_snapshot = build_manager_snapshot()
    runtime = runtime_state()
    rss_state = runtime.get("rss", {})
    last_poll_raw = rss_state.get("last_run_at")
    last_poll = None
    if last_poll_raw:
        try:
            last_poll = datetime.fromisoformat(str(last_poll_raw)).timestamp()
        except ValueError:
            last_poll = None
    return {
        "sonarr": None
        if not manager_snapshot["sonarr"]["configured"]
        else {
            "ok": health["sonarr"]["ok"],
            "version": health["sonarr"]["version"],
            "error": health["sonarr"]["error"],
            "syncing": bool(sync_status().get("running")),
        },
        "radarr": None
        if not manager_snapshot["radarr"]["configured"]
        else {
            "ok": health["radarr"]["ok"],
            "version": health["radarr"]["version"],
            "error": health["radarr"]["error"],
        },
        "animeworld": health["animeworld"],
        "rss": {
            "enabled": settings.rss_enabled,
            "ok": None if settings.rss_enabled and not last_poll else (not bool(rss_state.get("last_error"))) if settings.rss_enabled else False,
            "last_poll": last_poll,
        },
        "uptime_seconds": int(time.time() - _app_start_time),
        "automap_running": bool(automap_status().get("running")),
    }


def build_dashboard_context() -> dict:
    catalog = build_catalog_snapshot(show_limit=250, movie_limit=250)
    shows = [_show_payload(show["id"]) for show in catalog["shows"]]
    movies = [_movie_payload(movie["id"]) for movie in catalog["movies"]]
    show_items = [item for item in shows if item]
    movie_items = [item for item in movies if item]

    mapped_count = 0
    unmapped_count = 0
    for show in show_items:
        for season in show.get("seasons", []):
            season_number = int(season.get("season_number", 0))
            if season_number <= 0:
                continue
            ignored = bool(season.get("ignored"))
            aired = season_has_aired(season)
            has_mapping = bool(show.get("mappings", {}).get(season_number))
            if aired:
                if ignored:
                    continue
                if has_mapping:
                    mapped_count += 1
                else:
                    unmapped_count += 1
            elif has_mapping:
                mapped_count += 1

    return {
        "shows": show_items,
        "movies": movie_items,
        "mapped_count": mapped_count,
        "unmapped_count": unmapped_count,
        "automap_running": bool(automap_status().get("running")),
        "app_version": os.getenv("APP_VERSION", ""),
        "sonarr_sync_in_progress": bool(sync_status().get("running")),
        "api_key": settings.awc_api_key,
        "aw_base_url": settings.aw_base_url,
    }


def build_dashboard_html() -> str:
    template = _jinja.get_template("index.html")
    return template.render(**build_dashboard_context())
