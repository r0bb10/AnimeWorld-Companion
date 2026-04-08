"""RSS cache views and maintenance for the clean rebuild."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

import requests

from ..core.config import settings
from ..core.log_events import log_debug, log_exception, log_info, log_warning
from ..core.logging import get_logger
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
from ..repositories.rss_cache import (
    cleanup_rss_items,
    clear_rss_items,
    has_movie_rss_item,
    has_rss_item,
    list_rss_items,
    save_movie_rss_item,
    save_rss_item,
)
from .search_service import build_movie_search_items, build_show_search_items

logger = get_logger("rss")


def build_rss_snapshot(limit: int = 100) -> dict:
    items = list_rss_items(limit=limit)
    return {
        "count": len(items),
        "items": items,
    }


def clear_rss_cache() -> dict:
    removed = clear_rss_items()
    return {"removed": removed}


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


def _resolve_show_rss_mapping(anime_slug: str) -> dict | None:
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

    mapping = _resolve_show_rss_mapping(anime_slug)
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
    anime_slug = client.url_to_slug(fields["anime_link"])
    cached = 0

    mapping = _resolve_show_rss_mapping(anime_slug)
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
        futures = {pool.submit(_process_rss_item, fields, client): fields for fields in fields_list}
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
    return {"enabled": True, "cached": cached}
