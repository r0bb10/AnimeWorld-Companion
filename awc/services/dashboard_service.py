"""Legacy-style dashboard rendering backed by the rebuilt core."""

from datetime import UTC, datetime
import json
from pathlib import Path
import os
import time

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core.config import settings
from ..domain.release_window import has_started, utc_today_iso
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
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
    return has_started((season or {}).get("air_date_start"))


def movie_has_released(movie: dict) -> bool:
    return has_started((movie or {}).get("first_aired"))


def slug_to_url(slug: str) -> str:
    return AnimeWorldClient().slug_to_url(slug)


_jinja.globals["season_has_aired"] = season_has_aired
_jinja.globals["movie_has_released"] = movie_has_released
_jinja.globals["slug_to_url"] = slug_to_url


def _today_iso() -> str:
    return utc_today_iso()


def count_dashboard_to_map() -> int:
    today = _today_iso()
    with get_db() as conn:
        show_row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM show_seasons ss
            LEFT JOIN aw_show_mappings asm
                ON asm.show_id = ss.show_id
               AND asm.season_number = ss.season_number
            WHERE ss.season_number > 0
              AND ss.ignored = 0
              AND COALESCE(substr(ss.air_date_start, 1, 10), '') != ''
              AND substr(ss.air_date_start, 1, 10) <= ?
              AND asm.id IS NULL
            """,
            (today,),
        ).fetchone()
        movie_row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM movies m
            LEFT JOIN aw_movie_mappings amm ON amm.movie_id = m.id
            WHERE m.ignored = 0
              AND COALESCE(substr(m.first_aired, 1, 10), '') != ''
              AND substr(m.first_aired, 1, 10) <= ?
              AND amm.id IS NULL
            """,
            (today,),
        ).fetchone()
    return int(show_row["count"]) + int(movie_row["count"])


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


def _mapping_is_preaired(mapping: dict | None) -> bool:
    if not mapping:
        return False
    try:
        factors = json.loads(mapping.get("confidence_factors") or "{}")
    except (TypeError, ValueError):
        return False
    return bool(factors.get("preaired") or factors.get("preaired_placeholder"))


def _show_card(show: dict) -> dict:
    mappings = show.get("mappings", {})
    totals = {
        "total": 0,
        "active": 0,
        "mapped": 0,
        "ignored": 0,
        "unaired": 0,
    }
    rows: list[dict] = []
    for season in sorted(show.get("seasons", []), key=lambda item: item.get("season_number", 0)):
        season_number = int(season.get("season_number", 0) or 0)
        if season_number <= 0:
            continue
        aired = season_has_aired(season)
        ignored = bool(season.get("ignored"))
        mapping_list = mappings.get(season_number) or []
        has_mapping = bool(mapping_list)
        if aired:
            totals["total"] += 1
            if ignored:
                totals["ignored"] += 1
            else:
                totals["active"] += 1
                if has_mapping:
                    totals["mapped"] += 1
        else:
            totals["unaired"] += 1
            if ignored:
                totals["ignored"] += 1
            elif has_mapping:
                totals["total"] += 1
                totals["active"] += 1
                totals["mapped"] += 1

        if ignored:
            status_badge = {"class": "badge-ignored", "text": "ignored"}
        elif has_mapping:
            first_mapping = mapping_list[0]
            confidence = first_mapping.get("confidence_score")
            if not aired:
                text = "pre"
                if first_mapping.get("mapping_type") == "auto" and confidence:
                    text += f" {int(confidence * 100)}%"
                status_badge = {"class": "badge-prerelease", "text": text}
            else:
                text = str(first_mapping.get("mapping_type") or "mapped")
                if first_mapping.get("mapping_type") == "auto" and confidence:
                    text += f" {int(confidence * 100)}%"
                status_badge = {"class": f"badge-{first_mapping.get('mapping_type')}", "text": text}
        elif not aired:
            status_badge = {"class": "badge-unaired", "text": "unaired"}
        else:
            status_badge = None

        row_links = []
        for index, mapping in enumerate(mapping_list, start=1):
            episode_meta = ""
            episode_count = mapping.get("aw_episode_count")
            total_episodes = mapping.get("aw_total_episodes")
            if episode_count:
                episode_meta = f"{episode_count} eps"
                try:
                    if total_episodes and int(total_episodes) > int(episode_count):
                        episode_meta += f" (+{int(total_episodes) - int(episode_count)} specials)"
                except (TypeError, ValueError):
                    pass
            row_links.append(
                {
                    "part": mapping.get("part") or index,
                    "show_part": len(mapping_list) > 1,
                    "aw_link": str(mapping.get("aw_link") or ""),
                    "url": slug_to_url(str(mapping.get("aw_link") or "")),
                    "episode_meta": episode_meta,
                }
            )

        rows.append(
            {
                "item_kind": "show",
                "item_id": int(show["id"]),
                "season_number": season_number,
                "label": f"S{season_number:02d}",
                "mapped": has_mapping,
                "ignored": ignored,
                "aired": aired,
                "status_badge": status_badge,
                "links": row_links,
                "map_placeholder": "Paste AW link(s) (separate with newline/comma)...",
            }
        )

    effective_total = totals["active"]
    if effective_total == 0 and totals["ignored"] > 0:
        status = {"key": "ignored", "label": "ignored"}
    elif totals["mapped"] == effective_total and effective_total > 0:
        status = {"key": "mapped", "label": "all mapped"}
    elif totals["mapped"] > 0:
        status = {"key": "partial", "label": f"{totals['mapped']}/{effective_total}"}
    else:
        status = {"key": "unmapped", "label": "unmapped"}

    return {
        "kind": "show",
        "id": int(show["id"]),
        "title": str(show.get("title") or ""),
        "alternate_titles": list(show.get("alternate_titles") or []),
        "filter_text": " ".join(
            [str(show.get("title") or ""), *[str(item) for item in list(show.get("alternate_titles") or [])]]
        ).casefold(),
        "manager_label": "Sonarr",
        "manager_badge_class": "badge-sonarr",
        "meta_label": f"{totals['total']} seasons",
        "status": status,
        "has_unaired": totals["unaired"] > 0,
        "rows": rows,
        "discover_target": int(show["id"]),
        "discover_panel_id": f"disc-show-{show['id']}",
        "automap_label": "Automap This Show",
        "delete_label": "Delete Show",
    }


def _movie_card(movie: dict) -> dict:
    mapping = movie.get("mapping")
    ignored = bool(movie.get("ignored"))
    released = movie_has_released(movie)
    row_links = []
    if mapping:
        row_links.append(
            {
                "part": 1,
                "show_part": False,
                "aw_link": str(mapping.get("aw_link") or ""),
                "url": slug_to_url(str(mapping.get("aw_link") or "")),
                "episode_meta": "",
            }
        )
    status_badge = {"class": "badge-ignored", "text": "ignored"} if ignored else None
    if not ignored and mapping:
        confidence = mapping.get("confidence_score")
        if _mapping_is_preaired(mapping):
            text = "pre"
            if mapping.get("mapping_type") == "auto" and confidence:
                text += f" {int(confidence * 100)}%"
            status_badge = {"class": "badge-prerelease", "text": text}
        else:
            text = str(mapping.get("mapping_type") or "mapped")
            if mapping.get("mapping_type") == "auto" and confidence:
                text += f" {int(confidence * 100)}%"
            status_badge = {"class": f"badge-{mapping.get('mapping_type')}", "text": text}
    elif not ignored and not released:
        status_badge = {"class": "badge-unaired", "text": "unaired"}
    return {
        "kind": "movie",
        "id": int(movie["id"]),
        "title": str(movie.get("title") or ""),
        "alternate_titles": list(movie.get("alternate_titles") or []),
        "filter_text": " ".join(
            [str(movie.get("title") or ""), *[str(item) for item in list(movie.get("alternate_titles") or [])]]
        ).casefold(),
        "manager_label": "Radarr",
        "manager_badge_class": "badge-radarr",
        "meta_label": str(movie.get("year") or ""),
        "status": (
            {"key": "ignored", "label": "ignored"}
            if ignored and not mapping
            else {"key": "mapped", "label": "all mapped"} if mapping
            else {"key": "unmapped", "label": "unmapped"}
        ),
        "has_unaired": not released,
        "rows": [
            {
                "item_kind": "movie",
                "item_id": int(movie["id"]),
                "season_number": None,
                "label": "Film",
                "mapped": bool(mapping),
                "ignored": ignored,
                "aired": released,
                "status_badge": status_badge,
                "links": row_links,
                "map_placeholder": "Paste AW movie link...",
            }
        ],
        "discover_target": int(movie["id"]),
        "discover_panel_id": f"disc-movie-{movie['id']}",
        "automap_label": "Automap",
        "delete_label": "Delete Movie",
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


def build_dashboard_card(kind: str, item_id: int) -> dict | None:
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind == "show":
        show = _show_payload(item_id)
        return _show_card(show) if show else None
    if normalized_kind == "movie":
        movie = _movie_payload(item_id)
        return _movie_card(movie) if movie else None
    return None


def _build_dashboard_stats_context(catalog: dict | None = None) -> dict:
    snapshot = catalog or build_catalog_snapshot(show_limit=0, movie_limit=0)
    counts = snapshot.get("counts", {})
    return {
        "show_count": int(counts.get("shows") or 0),
        "movie_count": int(counts.get("movies") or 0),
        "to_map_count": count_dashboard_to_map(),
    }


def build_dashboard_context() -> dict:
    catalog = build_catalog_snapshot(show_limit=250, movie_limit=250)
    stats_context = _build_dashboard_stats_context(catalog)
    shows = [_show_payload(show["id"]) for show in catalog["shows"]]
    movies = [_movie_payload(movie["id"]) for movie in catalog["movies"]]
    show_items = [item for item in shows if item]
    movie_items = [item for item in movies if item]
    library_items = [_show_card(item) for item in show_items] + [_movie_card(item) for item in movie_items]
    library_items.sort(key=lambda item: (str(item.get("title") or "").casefold(), item.get("kind") != "show"))

    return {
        "shows": show_items,
        "movies": movie_items,
        "library_items": library_items,
        **stats_context,
        "automap_running": bool(automap_status().get("running")),
        "app_version": os.getenv("APP_VERSION", ""),
        "sonarr_sync_in_progress": bool(sync_status().get("running")),
        "api_key": settings.awc_api_key,
        "aw_base_url": settings.aw_base_url,
    }


def build_dashboard_html() -> str:
    template = _jinja.get_template("index.html")
    return template.render(**build_dashboard_context())


def build_dashboard_stats_html() -> str:
    template = _jinja.get_template("_dashboard_stats.html")
    return template.render(**_build_dashboard_stats_context())


def build_dashboard_card_html(kind: str, item_id: int) -> str | None:
    card = build_dashboard_card(kind, item_id)
    if not card:
        return None
    template = _jinja.get_template("_dashboard_card.html")
    return template.render(card=card)
