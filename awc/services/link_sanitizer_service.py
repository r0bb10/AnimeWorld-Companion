"""Mapped AnimeWorld link verification and refresh."""

from __future__ import annotations

from datetime import UTC, datetime
import threading

import requests

from ..core.config import settings
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.db import get_db

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


def sanitize_links_once() -> dict:
    client = AnimeWorldClient()
    now = datetime.now(UTC).isoformat()
    result = {"checked": 0, "updated": 0, "removed": 0, "failed": 0}

    with get_db(write=True) as conn:
        show_rows = conn.execute(
            """
            SELECT id, aw_link, link_check_failures
            FROM aw_show_mappings
            ORDER BY id
            """
        ).fetchall()
        for row in show_rows:
            result["checked"] += 1
            try:
                new_slug, _ = _verify_slug(client, row["aw_link"])
                conn.execute(
                    """
                    UPDATE aw_show_mappings
                    SET aw_link = ?, link_check_failures = 0, last_verified = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_slug, now, now, row["id"]),
                )
                if new_slug != row["aw_link"]:
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

        movie_rows = conn.execute(
            """
            SELECT id, aw_link, link_check_failures
            FROM aw_movie_mappings
            ORDER BY id
            """
        ).fetchall()
        for row in movie_rows:
            result["checked"] += 1
            try:
                new_slug, _ = _verify_slug(client, row["aw_link"])
                conn.execute(
                    """
                    UPDATE aw_movie_mappings
                    SET aw_link = ?, link_check_failures = 0, last_verified = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_slug, now, now, row["id"]),
                )
                if new_slug != row["aw_link"]:
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
