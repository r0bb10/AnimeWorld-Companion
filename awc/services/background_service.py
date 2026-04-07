"""Background runtime loops for sync, RSS, and lightweight reconciliation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

import requests

from ..core.config import settings
from ..core.log_events import log_block, log_debug, log_exception, log_info, log_warning
from ..core.logging import get_logger
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
from ..repositories.rss_cache import cleanup_rss_items, has_movie_rss_item, has_rss_item, save_movie_rss_item, save_rss_item
from .download_service import reconcile_vanished_downloads, restore_on_startup
from .eligible_service import run_eligible_once
from .sanitizer_service import sanitize_links_once, sanitizer_status
from .search_service import build_movie_search_items, build_show_search_items
from .sync_runner_service import sync_all

logger = get_logger("runtime")

_stop_event = threading.Event()
_threads: dict[str, threading.Thread] = {}
_state_lock = threading.Lock()
_state = {
    "sync": {"running": False, "last_run_at": None, "last_error": ""},
    "rss": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_cached": 0},
    "links": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "eligible": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "scanner": {"enabled": True, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "startup": {"restored": 0, "fixed": 0},
}


def _minutes_label(seconds: int) -> str:
    if int(seconds) < 60:
        return f"{int(seconds)}s"
    return f"{max(1, int(seconds) // 60)}m"


def runtime_state() -> dict:
    with _state_lock:
        return json.loads(json.dumps(_state))


def _set_state(section: str, **updates) -> None:
    with _state_lock:
        _state.setdefault(section, {}).update(updates)


def _rss_feed_url() -> str:
    if not settings.aw_base_url:
        return ""
    return f"{settings.aw_base_url.rstrip('/')}/rss/episodes"


def _extract_rss_fields(item: ET.Element) -> dict | None:
    anime_link = ""
    episode_number = None

    for child in item:
        tag = child.tag.lower()
        if "link" in tag and "anime" in tag:
            anime_link = (child.text or "").strip()
        elif "number" in tag and "animeworld" in tag:
            try:
                episode_number = int((child.text or "").strip())
            except ValueError:
                return None

    if not anime_link:
        link = (item.findtext("link") or "").strip()
        if "/play/" in link:
            anime_link = link.split("/play/", 1)[0] + "/play/" + link.split("/play/", 1)[1].split("/")[0] + "/"

    if not anime_link or episode_number is None:
        return None

    return {
        "anime_link": anime_link.rstrip("/") + "/",
        "episode_number": episode_number,
        "pub_date": (item.findtext("pubDate") or "").strip(),
    }


def _resolve_rss_mapping(anime_slug: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                m.show_id,
                m.season_number,
                m.part,
                m.aw_link,
                m.aw_episode_count,
                s.title,
                s.tvdb_id
            FROM aw_show_mappings m
            JOIN shows s ON s.id = m.show_id
            WHERE m.aw_link = ?
            ORDER BY m.part
            LIMIT 1
            """,
            (anime_slug,),
        ).fetchone()
    return dict(row) if row else None


def _resolve_movie_rss_mapping(anime_slug: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                m.movie_id,
                m.aw_link,
                mv.title,
                mv.tmdb_id,
                mv.imdb_id
            FROM aw_movie_mappings m
            JOIN movies mv ON mv.id = m.movie_id
            WHERE m.aw_link = ?
            LIMIT 1
            """,
            (anime_slug,),
        ).fetchone()
    return dict(row) if row else None


def _resolve_rss_episode(show_id: int, anime_slug: str, episode_number: int) -> tuple[int, int] | None:
    with get_db() as conn:
        linked = conn.execute(
            """
            SELECT COUNT(DISTINCT season_number) AS season_count
            FROM aw_show_mappings
            WHERE show_id = ? AND aw_link = ?
            """,
            (show_id, anime_slug),
        ).fetchone()
    if linked and linked["season_count"] > 1:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT internal_season, internal_episode
                FROM show_scene_episodes
                WHERE show_id = ? AND absolute_episode = ?
                LIMIT 1
                """,
                (show_id, episode_number),
            ).fetchone()
        if row:
            return row["internal_season"], row["internal_episode"]
        return None

    mapping = _resolve_rss_mapping(anime_slug)
    if not mapping:
        return None

    season_number = mapping["season_number"]
    with get_db() as conn:
        parts = conn.execute(
            """
            SELECT part, aw_link, aw_episode_count
            FROM aw_show_mappings
            WHERE show_id = ? AND season_number = ?
            ORDER BY part
            """,
            (show_id, season_number),
        ).fetchall()

    resolved_episode = episode_number
    if len(parts) > 1:
        cumulative = 0
        for part in parts:
            if part["aw_link"] == anime_slug:
                resolved_episode += cumulative
                break
            cumulative += part["aw_episode_count"] or 0

    return season_number, resolved_episode


def _process_rss_item(fields: dict, client: AnimeWorldClient) -> int:
    """Process one parsed RSS item. Returns number of entries cached (0 or more)."""
    anime_slug = client.url_to_slug(fields["anime_link"])
    cached = 0

    mapping = _resolve_rss_mapping(anime_slug)
    if mapping:
        resolved = _resolve_rss_episode(mapping["show_id"], anime_slug, fields["episode_number"])
        if not resolved:
            return 0
        season_number, episode_number = resolved
        if has_rss_item(mapping["show_id"], season_number, episode_number):
            return 0
        items = build_show_search_items(
            mapping["title"],
            season_number,
            episode_number,
            tvdb_id=mapping.get("tvdb_id"),
        )
        if not items:
            return 0
        item_payload = items[0]
        if save_rss_item(
            show_id=mapping["show_id"],
            season_number=season_number,
            episode_number=episode_number,
            title=item_payload["title"],
            guid=item_payload["guid"],
            size=int(item_payload.get("size", 0) or 0),
            pub_date=fields["pub_date"],
            aw_episode_link=item_payload.get("aw_link", anime_slug),
        ):
            cached += 1
        return cached

    movie_mapping = _resolve_movie_rss_mapping(anime_slug)
    if not movie_mapping:
        return 0
    items = build_movie_search_items(
        movie_mapping["title"],
        tmdb_id=movie_mapping.get("tmdb_id"),
        imdb_id=movie_mapping.get("imdb_id") or "",
    )
    for item_payload in items:
        guid = str(item_payload.get("guid", "") or "")
        if not guid or has_movie_rss_item(movie_mapping["movie_id"], guid):
            continue
        if save_movie_rss_item(
            movie_id=movie_mapping["movie_id"],
            title=item_payload["title"],
            guid=guid,
            size=int(item_payload.get("size", 0) or 0),
            pub_date=fields["pub_date"],
            aw_episode_link=item_payload.get("aw_link", anime_slug),
        ):
            cached += 1
    return cached


def update_rss_cache(*, emit_cycle_logs: bool = False) -> dict:
    if not settings.rss_enabled:
        _set_state("rss", enabled=False)
        return {"enabled": False, "cached": 0}

    url = _rss_feed_url()
    if not url:
        return {"enabled": True, "cached": 0, "error": "missing_aw_base_url"}

    client = AnimeWorldClient()

    if emit_cycle_logs:
        log_debug(logger, "runtime.rss.cycle.started", "RSS poll cycle started")

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except requests.ConnectionError:
        log_warning(logger, "runtime.rss.skipped", "RSS poll skipped: AnimeWorld unreachable")
        result = {"enabled": True, "cached": 0, "error": "unreachable"}
        if emit_cycle_logs:
            log_debug(logger, "runtime.rss.cycle.finished", "RSS poll cycle finished", details={"cached": 0, "error": "unreachable"})
        return result
    except requests.Timeout:
        log_warning(logger, "runtime.rss.skipped", "RSS poll skipped: request timed out")
        result = {"enabled": True, "cached": 0, "error": "timeout"}
        if emit_cycle_logs:
            log_debug(logger, "runtime.rss.cycle.finished", "RSS poll cycle finished", details={"cached": 0, "error": "timeout"})
        return result
    except Exception as exc:
        log_exception(logger, "runtime.rss.failed", "RSS fetch failed", details={"error": str(exc)})
        result = {"enabled": True, "cached": 0, "error": str(exc)}
        if emit_cycle_logs:
            log_debug(logger, "runtime.rss.cycle.finished", "RSS poll cycle finished", details={"cached": 0, "error": str(exc)})
        return result

    fields_list = []
    for item in root.findall(".//item"):
        try:
            fields = _extract_rss_fields(item)
            if fields:
                fields_list.append(fields)
        except Exception:
            log_exception(logger, "runtime.rss.item_parse_failed", "RSS item parse failed")

    cached = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_process_rss_item, f, client): f for f in fields_list}
        for future in as_completed(futures):
            try:
                cached += future.result()
            except Exception:
                log_exception(logger, "runtime.rss.item_processing_failed", "RSS item processing failed")

    cleanup_rss_items(settings.rss_cache_retention_days)
    if cached:
        log_info(logger, "runtime.rss.cached", "RSS cached items", details={"cached": cached}, lines=[f"cached={cached}"])
    if emit_cycle_logs:
        log_debug(logger, "runtime.rss.cycle.finished", "RSS poll cycle finished", details={"cached": cached})
    _set_state(
        "rss",
        enabled=True,
        running=False,
        last_run_at=datetime.now(UTC).isoformat(),
        last_error="",
        last_cached=cached,
    )
    return {"enabled": True, "cached": cached}


def _run_rss_loop() -> None:
    _set_state("rss", enabled=settings.rss_enabled, running=True)
    interval = max(30, settings.rss_poll_interval)
    log_info(
        logger,
        "runtime.rss.loop_started",
        "RSS poller started",
        details={"interval_seconds": interval},
        lines=[f"interval={_minutes_label(interval)}"],
    )
    while not _stop_event.is_set():
        try:
            update_rss_cache(emit_cycle_logs=True)
        except Exception as exc:
            log_exception(logger, "runtime.rss.loop_failed", "RSS poller failed", details={"error": str(exc)})
            _set_state(
                "rss",
                enabled=settings.rss_enabled,
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
            )
        if _stop_event.wait(max(30, settings.rss_poll_interval)):
            break


def _run_sync_loop() -> None:
    _set_state("sync", running=True)
    interval = max(60, settings.sync_interval_minutes * 60)
    log_info(
        logger,
        "runtime.sync.loop_started",
        "Background sync loop started",
        details={"interval_seconds": interval},
        lines=[f"interval={_minutes_label(interval)}"],
    )
    if _stop_event.wait(60):  # brief startup grace before first sync
        return
    while not _stop_event.is_set():
        try:
            log_info(logger, "runtime.sync.started", "Background sync started")
            result = sync_all()
            log_info(
                logger,
                "runtime.sync.completed",
                "Background sync completed",
                details={"sonarr": int(result.get("sonarr", 0) or 0), "radarr": int(result.get("radarr", 0) or 0)},
                lines=[f"sonarr={int(result.get('sonarr', 0) or 0)}", f"radarr={int(result.get('radarr', 0) or 0)}"],
            )
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.sync.failed", "Background sync failed", details={"error": str(exc)})
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
            )
        if _stop_event.wait(interval):
            break


def _run_link_loop() -> None:
    _set_state("links", enabled=settings.sanitizer_enabled, running=False, last_error="")
    if not settings.sanitizer_enabled:
        log_info(logger, "runtime.links.disabled", "Sanitizer loop disabled by env")
        return
    first_run = 60 * 10
    interval = 60 * 60 * 24
    log_info(
        logger,
        "runtime.links.scheduled",
        "Sanitizer loop scheduled",
        lines=[f"first_run={_minutes_label(first_run)}", f"interval={_minutes_label(interval)}"],
        details={"first_run_seconds": first_run, "interval_seconds": interval},
    )
    if _stop_event.wait(first_run):
        return
    while not _stop_event.is_set():
        try:
            _set_state("links", running=True)
            result = sanitize_links_once()
            _set_state(
                "links",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.links.failed", "Link sanitizer failed", details={"error": str(exc)})
            _set_state(
                "links",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=sanitizer_status().get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def _run_eligible_loop() -> None:
    _set_state("eligible", enabled=settings.eligible_enabled, running=False, last_error="")
    if not settings.eligible_enabled:
        log_info(logger, "runtime.eligible.disabled", "Eligible loop disabled by env")
        return
    interval = max(60 * 60, int(settings.eligible_interval or 0))
    log_info(
        logger,
        "runtime.eligible.scheduled",
        "Eligible loop scheduled",
        lines=[
            f"first_run={_minutes_label(300)}",
            f"interval={_minutes_label(interval)}",
            f"lookback_days={max(0, int(settings.eligible_lookback_days or 0))}",
        ],
        details={
            "first_run_seconds": 300,
            "interval_seconds": interval,
            "lookback_days": max(0, int(settings.eligible_lookback_days or 0)),
        },
    )
    if _stop_event.wait(300):
        return
    while not _stop_event.is_set():
        try:
            _set_state("eligible", running=True)
            result = run_eligible_once()
            _set_state(
                "eligible",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.eligible.failed", "Eligible loop failed", details={"error": str(exc)})
            _set_state(
                "eligible",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=runtime_state().get("eligible", {}).get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def _run_scanner_loop() -> None:
    first_run = 30
    interval = 30
    grace_seconds = 60

    _set_state("scanner", enabled=True, running=False, last_error="")
    log_info(
        logger,
        "runtime.scanner.scheduled",
        "Vanished scanner scheduled",
        lines=[
            f"first_run={_minutes_label(first_run)}",
            f"interval={_minutes_label(interval)}",
            f"grace={_minutes_label(grace_seconds)}",
        ],
        details={
            "first_run_seconds": first_run,
            "interval_seconds": interval,
            "grace_seconds": grace_seconds,
        },
    )
    if _stop_event.wait(first_run):
        return
    while not _stop_event.is_set():
        try:
            _set_state("scanner", running=True)
            result = reconcile_vanished_downloads()
            _set_state(
                "scanner",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.scanner.failed", "Vanished scanner failed", details={"error": str(exc)})
            _set_state(
                "scanner",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=runtime_state().get("scanner", {}).get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def start_background_workers() -> dict:
    _stop_event.clear()
    startup = restore_on_startup()
    _set_state("startup", **startup)
    _set_state("links", enabled=settings.sanitizer_enabled, running=False, last_error="")
    _set_state("eligible", enabled=settings.eligible_enabled, running=False, last_error="")
    _set_state("scanner", enabled=True, running=False, last_error="")

    workers = {
        "sync": _run_sync_loop,
        "scanner": _run_scanner_loop,
    }
    if settings.sanitizer_enabled:
        workers["links"] = _run_link_loop
    if settings.eligible_enabled:
        workers["eligible"] = _run_eligible_loop
    if settings.rss_enabled:
        workers["rss"] = _run_rss_loop

    started: list[str] = []
    for name, target in workers.items():
        thread = _threads.get(name)
        if thread and thread.is_alive():
            continue
        thread = threading.Thread(target=target, name=f"awc-{name}", daemon=True)
        _threads[name] = thread
        thread.start()
        started.append(name)

    log_block(
        logger,
        logging.INFO,
        "Background workers started",
        [f"workers={', '.join(started) if started else 'none'}"],
        event_type="runtime.workers.started",
        details={"workers": started},
    )
    return {"started": started, "startup": startup}


def stop_background_workers() -> None:
    _stop_event.set()
    for thread in list(_threads.values()):
        thread.join(timeout=1)
    log_info(logger, "runtime.workers.stopped", "Background workers stopped")
