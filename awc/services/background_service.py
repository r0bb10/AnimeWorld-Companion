"""Background runtime loops for sync, RSS, and import polling."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

from ..core.config import settings
from ..core.logging import get_logger
from ..integrations.animeworld_client import AnimeWorldClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.db import get_db
from ..repositories.rss_cache import cleanup_rss_items, has_rss_item, save_rss_item
from .download_service import completed_downloads, mark_imported, restore_on_startup
from .link_sanitizer_service import sanitize_links_once, sanitizer_status
from .search_service import build_show_search_items
from .sync_runner_service import sync_all

logger = get_logger(__name__)

_stop_event = threading.Event()
_threads: dict[str, threading.Thread] = {}
_state_lock = threading.Lock()
_state = {
    "sync": {"running": False, "last_run_at": None, "last_error": ""},
    "rss": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_cached": 0},
    "imports": {"running": False, "last_run_at": None, "last_error": "", "last_marked": 0},
    "links": {"running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "startup": {"restored": 0, "fixed": 0},
}


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


def update_rss_cache() -> dict:
    if not settings.rss_enabled:
        _set_state("rss", enabled=False)
        return {"enabled": False, "cached": 0}

    url = _rss_feed_url()
    if not url:
        return {"enabled": True, "cached": 0, "error": "missing_aw_base_url"}

    client = AnimeWorldClient()
    cached = 0
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    for item in root.findall(".//item"):
        fields = _extract_rss_fields(item)
        if not fields:
            continue
        anime_slug = client.url_to_slug(fields["anime_link"])
        mapping = _resolve_rss_mapping(anime_slug)
        if not mapping:
            continue
        resolved = _resolve_rss_episode(mapping["show_id"], anime_slug, fields["episode_number"])
        if not resolved:
            continue
        season_number, episode_number = resolved
        if has_rss_item(mapping["show_id"], season_number, episode_number):
            continue

        items = build_show_search_items(
            mapping["title"],
            season_number,
            episode_number,
            tvdb_id=mapping.get("tvdb_id"),
        )
        if not items:
            continue

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

    cleanup_rss_items(settings.rss_cache_retention_days)
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
    while not _stop_event.is_set():
        try:
            update_rss_cache()
        except Exception as exc:
            logger.exception("RSS poller failed")
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
    while not _stop_event.is_set():
        try:
            result = sync_all()
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            logger.exception("Background sync failed")
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
            )
        if _stop_event.wait(max(60, settings.sync_interval_minutes * 60)):
            break


def _run_import_loop() -> None:
    _set_state("imports", running=True)
    client = SonarrClient()
    while not _stop_event.is_set():
        marked = 0
        try:
            for entry in completed_downloads():
                sonarr_id = entry.get("sonarr_id")
                if not sonarr_id:
                    continue
                match = re.search(r"[Ss](\d+)[Ee](\d+)", entry.get("filename", ""))
                if not match:
                    continue
                season_number = int(match.group(1))
                episode_number = int(match.group(2))
                episodes = client.fetch_season_episodes(int(sonarr_id), season_number)
                target = next((ep for ep in episodes if ep.get("episodeNumber") == episode_number), None)
                if not target or not target.get("hasFile"):
                    continue
                if mark_imported(entry["id"]):
                    marked += 1
                    if settings.sonarr_unmonitor_imported and target.get("id"):
                        client.unmonitor_episode(int(target["id"]))
            _set_state(
                "imports",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_marked=marked,
            )
        except Exception as exc:
            logger.exception("Import poller failed")
            _set_state(
                "imports",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_marked=marked,
            )
        if _stop_event.wait(max(30, settings.sonarr_import_poll_interval)):
            break


def _run_link_loop() -> None:
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
            logger.exception("Link sanitizer failed")
            _set_state(
                "links",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=sanitizer_status().get("last_result"),
            )
        if _stop_event.wait(60 * 60 * 24):
            break


def start_background_workers() -> dict:
    _stop_event.clear()
    startup = restore_on_startup()
    _set_state("startup", **startup)

    workers = {
        "sync": _run_sync_loop,
        "imports": _run_import_loop,
        "links": _run_link_loop,
    }
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

    return {"started": started, "startup": startup}


def stop_background_workers() -> None:
    _stop_event.set()
    for thread in list(_threads.values()):
        thread.join(timeout=1)
