"""Mapped AnimeWorld link verification and refresh."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import threading

import requests

from ..core.config import settings
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .automap_language import resolve_movie_language_preference, resolve_show_language_preference
from .automap_scoring import calculate_movie_confidence, calculate_show_confidence, parse_italian_date

_state_lock = threading.Lock()
_state = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": "",
    "last_result": None,
}


def sanitizer_status() -> dict:
    with _state_lock:
        return dict(_state)


def _set_state(**updates) -> None:
    with _state_lock:
        _state.update(updates)


def _verify_slug(client: AnimeWorldClient, slug: str) -> tuple[str, int]:
    url = client.slug_to_url(slug)
    response = requests.get(url, timeout=15, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    final_slug = client.url_to_slug(response.url)
    return final_slug or slug, response.status_code


def _has_aired(season: dict) -> bool:
    start = str((season or {}).get("air_date_start") or "").strip()
    if not start:
        return False
    return start[:10] <= datetime.now(UTC).date().isoformat()


def _refresh_show_mapping_metadata(client: AnimeWorldClient, row: dict, new_slug: str, now: str) -> bool:
    show = get_show_detail(int(row["show_id"]))
    if not show:
        return False
    season = next((item for item in show.get("seasons", []) if int(item.get("season_number") or 0) == int(row["season_number"])), None)
    if not season:
        return False

    info, episodes, _, is_placeholder = client.get_info_and_episodes_meta(new_slug)
    non_special, total, _ = client.count_non_special_episodes(episodes)
    release_value = str(info.get("Data di Uscita") or "")
    release_dt = parse_italian_date(release_value) if release_value else None
    candidate = {
        "aw_link": new_slug,
        "aw_title": row.get("aw_title") or show.get("title") or "",
        "aw_jtitle": "",
        "aw_status": str(info.get("Stato") or row.get("aw_status") or ""),
        "aw_category": str(info.get("Categoria") or row.get("aw_category") or ""),
        "aw_audio": str(info.get("Audio") or ""),
        "aw_year": release_dt.year if release_dt else show.get("year"),
        "aw_release_datetime": release_dt,
        "aw_episode_count": non_special,
        "aw_total_episodes": total,
        "aw_is_placeholder": is_placeholder,
    }
    season_payload = {**season, "has_aired": _has_aired(season)}
    want_dubbed = resolve_show_language_preference(show)
    score, factors = calculate_show_confidence(show, season_payload, candidate, want_dubbed=want_dubbed)

    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_show_mappings
            SET aw_link = ?,
                aw_status = ?,
                aw_category = ?,
                aw_episode_count = ?,
                aw_total_episodes = ?,
                confidence_score = ?,
                confidence_factors = ?,
                link_check_failures = 0,
                last_verified = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_slug,
                candidate["aw_status"],
                candidate["aw_category"],
                candidate["aw_episode_count"],
                candidate["aw_total_episodes"],
                score,
                json.dumps(factors),
                now,
                now,
                row["id"],
            ),
        )
    return new_slug != row["aw_link"] or bool(json.loads(row.get("confidence_factors") or "{}").get("preaired")) != bool(factors.get("preaired_placeholder"))


def _refresh_movie_mapping_metadata(client: AnimeWorldClient, row: dict, new_slug: str, now: str) -> bool:
    movie = get_movie_detail(int(row["movie_id"]))
    if not movie:
        return False

    info, episodes, _, is_placeholder = client.get_info_and_episodes_meta(new_slug)
    non_special, total, _ = client.count_non_special_episodes(episodes)
    release_value = str(info.get("Data di Uscita") or "")
    release_dt = parse_italian_date(release_value) if release_value else None
    candidate = {
        "aw_link": new_slug,
        "aw_title": row.get("aw_title") or movie.get("title") or "",
        "aw_jtitle": "",
        "aw_status": str(info.get("Stato") or row.get("aw_status") or ""),
        "aw_category": str(info.get("Categoria") or row.get("aw_category") or ""),
        "aw_audio": str(info.get("Audio") or ""),
        "aw_year": release_dt.year if release_dt else movie.get("year"),
        "aw_release_datetime": release_dt,
        "aw_episode_count": non_special,
        "aw_total_episodes": total,
        "aw_is_placeholder": is_placeholder,
    }
    want_dubbed = resolve_movie_language_preference(movie)
    score, factors = calculate_movie_confidence(movie, candidate, want_dubbed=want_dubbed)

    with get_db(write=True) as conn:
        conn.execute(
            """
            UPDATE aw_movie_mappings
            SET aw_link = ?,
                aw_status = ?,
                aw_category = ?,
                confidence_score = ?,
                confidence_factors = ?,
                link_check_failures = 0,
                last_verified = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_slug,
                candidate["aw_status"],
                candidate["aw_category"],
                score,
                json.dumps(factors),
                now,
                now,
                row["id"],
            ),
        )
    return new_slug != row["aw_link"]


def _needs_show_mapping_refresh(show: dict, season: dict) -> bool:
    mappings = list(season.get("mappings") or [])
    if not mappings:
        return False
    if any((mapping.get("mapping_type") or "") != "auto" for mapping in mappings):
        return False

    markers = [dict(item) for item in (season.get("segment_markers") or []) if int(item.get("count") or 0) > 0]
    if len(markers) <= 1:
        return False

    ordered_mappings = sorted(mappings, key=lambda item: int(item.get("part") or 0))
    marker_counts = [int(item.get("count") or 0) for item in markers]
    mapping_counts = [int(item.get("aw_episode_count") or 0) for item in ordered_mappings]
    if len(marker_counts) != len(mapping_counts):
        return True
    if any(abs(left - right) > 1 for left, right in zip(marker_counts, mapping_counts)):
        return True

    season_total = int(season.get("episode_count") or 0)
    if season_total and abs(sum(mapping_counts) - season_total) > 1:
        return True

    factors = []
    for mapping in ordered_mappings:
        try:
            factors.append(json.loads(mapping.get("confidence_factors") or "{}"))
        except (TypeError, ValueError):
            factors.append({})
    return not all(bool(item.get("split_cour")) for item in factors)


def sanitize_links_once() -> dict:
    client = AnimeWorldClient()
    now = datetime.now(UTC).isoformat()
    result = {"checked": 0, "updated": 0, "removed": 0, "failed": 0}
    show_seasons_to_refresh: set[tuple[int, int]] = set()

    with get_db(write=True) as conn:
        show_rows = conn.execute(
            """
            SELECT id, show_id, season_number, aw_link, aw_title, aw_status, aw_category, confidence_factors, link_check_failures
            FROM aw_show_mappings
            ORDER BY id
            """
        ).fetchall()
        for row in show_rows:
            result["checked"] += 1
            try:
                new_slug, _ = _verify_slug(client, row["aw_link"])
                if _refresh_show_mapping_metadata(client, dict(row), new_slug, now):
                    result["updated"] += 1
            except Exception:
                failures = int(row["link_check_failures"] or 0) + 1
                if failures >= 2:
                    conn.execute("DELETE FROM aw_show_mappings WHERE id = ?", (row["id"],))
                    result["removed"] += 1
                else:
                    conn.execute(
                        """
                        UPDATE aw_show_mappings
                        SET link_check_failures = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (failures, now, row["id"]),
                    )
                    result["failed"] += 1

        for row in conn.execute(
            """
            SELECT DISTINCT show_id, season_number
            FROM aw_show_mappings
            WHERE mapping_type = 'auto'
            ORDER BY show_id, season_number
            """
        ).fetchall():
            show_seasons_to_refresh.add((int(row["show_id"]), int(row["season_number"])))

        movie_rows = conn.execute(
            """
            SELECT id, movie_id, aw_link, aw_title, aw_status, aw_category, confidence_factors, link_check_failures
            FROM aw_movie_mappings
            ORDER BY id
            """
        ).fetchall()
        for row in movie_rows:
            result["checked"] += 1
            try:
                new_slug, _ = _verify_slug(client, row["aw_link"])
                if _refresh_movie_mapping_metadata(client, dict(row), new_slug, now):
                    result["updated"] += 1
            except Exception:
                failures = int(row["link_check_failures"] or 0) + 1
                if failures >= 2:
                    conn.execute("DELETE FROM aw_movie_mappings WHERE id = ?", (row["id"],))
                    result["removed"] += 1
                else:
                    conn.execute(
                        """
                        UPDATE aw_movie_mappings
                        SET link_check_failures = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (failures, now, row["id"]),
                    )
                    result["failed"] += 1

    for show_id, season_number in sorted(show_seasons_to_refresh):
        show = get_show_detail(show_id)
        if not show:
            continue
        season = next((item for item in show.get("seasons", []) if int(item.get("season_number") or 0) == season_number), None)
        if not season or not _needs_show_mapping_refresh(show, season):
            continue
        from .automap_service import automap_show

        response = automap_show(show_id, season_number=season_number, force=True)
        if response.get("status") in {"success", "partial"}:
            result["updated"] += 1

    _set_state(last_result=result, last_error="", last_finished_at=now, running=False)
    return result


def start_link_sanitizer() -> dict:
    def worker():
        _set_state(running=True, last_started_at=datetime.now(UTC).isoformat(), last_error="")
        try:
            sanitize_links_once()
        except Exception as exc:
            _set_state(last_error=str(exc), last_finished_at=datetime.now(UTC).isoformat(), running=False)
            raise

    threading.Thread(target=worker, name="awc-link-sanitizer", daemon=True).start()
    return {"ok": True, "message": "Mapped links sanitizer started"}
