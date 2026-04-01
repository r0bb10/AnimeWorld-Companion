"""Fake torrent handoff and background download tracking for the clean rebuild."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
import os
import threading
from urllib.parse import urlencode

import requests

from ..core.config import settings
from ..domain.media import MediaKind, MediaManager, NamingContext
from ..repositories.downloads import (
    clear_finished_downloads,
    create_download,
    delete_download,
    get_download,
    list_all_downloads,
    list_completed_downloads,
    list_downloads,
    update_download_progress,
    update_download_status,
)
from .naming_service import build_release_name

_download_events: dict[str, threading.Event] = {}
_download_threads: dict[str, threading.Thread] = {}
_download_metrics: dict[str, dict[str, float]] = {}
_download_lock = threading.Lock()
_download_semaphore = threading.Semaphore(max(1, settings.max_concurrent_downloads))


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return f"{len(value)}:".encode() + value
    if isinstance(value, str):
        data = value.encode()
        return f"{len(data)}:".encode() + data
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        payload = []
        for key in sorted(value):
            payload.append(_bencode(str(key)))
            payload.append(_bencode(value[key]))
        return b"d" + b"".join(payload) + b"e"
    raise TypeError(f"Unsupported bencode value: {type(value)!r}")


def _download_base_url() -> str:
    return settings.awc_url


def _encode_source_token(source: str) -> str:
    return urlsafe_b64encode(source.encode()).decode().rstrip("=")


def _decode_source_token(token: str) -> str:
    if not token:
        return ""
    padding = "=" * (-len(token) % 4)
    try:
        return urlsafe_b64decode(f"{token}{padding}".encode()).decode()
    except Exception:
        return token


def _part_path(filename: str) -> str:
    data_root = Path(settings.data_path)
    data_root.mkdir(parents=True, exist_ok=True)
    return str(data_root / f"{filename}.part")


def _final_path(filename: str) -> str:
    data_root = Path(settings.data_path)
    data_root.mkdir(parents=True, exist_ok=True)
    return str(data_root / filename)


def _safe_unlink(path: str) -> None:
    target = _localize_data_path(path)
    if not target or not os.path.exists(target):
        return
    data_root = os.path.abspath(settings.data_path)
    target_abs = os.path.abspath(target)
    if os.path.commonpath([target_abs, data_root]) != data_root:
        return
    os.remove(target_abs)


def _localize_data_path(path: str) -> str:
    if not path:
        return ""
    data_root = Path(settings.data_path)
    if path.startswith("/data/"):
        return str(data_root / path.removeprefix("/data/"))
    return path


def _release_context(
    *,
    manager: str,
    title: str,
    season: int | None,
    episode: int | None,
    year: int | None,
) -> NamingContext:
    manager_kind = MediaManager((manager or "sonarr").lower())
    kind = MediaKind.SERIES if manager_kind is MediaManager.SONARR else MediaKind.MOVIE
    return NamingContext(
        manager=manager_kind,
        kind=kind,
        title=title,
        season_number=season,
        episode_number=episode,
        year=year,
    )


def build_download_url(
    *,
    manager: str,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    source: str = "",
    manager_id: int | None = None,
) -> str:
    params: dict[str, str | int] = {
        "manager": manager,
        "title": title,
    }
    if settings.awc_api_key:
        params["apikey"] = settings.awc_api_key
    if season is not None:
        params["season"] = season
    if episode is not None:
        params["episode"] = episode
    if year is not None:
        params["year"] = year
    if manager_id is not None:
        params["manager_id"] = manager_id
    if source:
        params["source"] = _encode_source_token(source)
    query = urlencode(params)
    return f"{_download_base_url().rstrip('/')}/download?{query}"


def create_fake_torrent(
    *,
    manager: str,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    source: str = "",
    manager_id: int | None = None,
) -> tuple[dict, bytes, str]:
    release_name = build_release_name(
        _release_context(
            manager=manager,
            title=title,
            season=season,
            episode=episode,
            year=year,
        )
    )
    resolved_source = _decode_source_token(source) or build_download_url(
        manager=manager,
        title=title,
        season=season,
        episode=episode,
        year=year,
        manager_id=manager_id,
    )
    existing = next(
        (
            item
            for item in list_all_downloads()
            if item.get("url") == resolved_source or item.get("filename") == release_name
        ),
        None,
    )
    if existing:
        download = existing
    else:
        download = create_download(
            url=resolved_source,
            filename=release_name,
            status="queued",
            part_path=_part_path(release_name),
            sonarr_id=manager_id if manager == "sonarr" else None,
            radarr_id=manager_id if manager == "radarr" else None,
        )
    queue_download(download["id"])

    token = sha1(
        f"{download.get('id')}|{release_name}|{datetime.now(UTC).isoformat()}".encode()
    ).hexdigest()
    info = {
        "name": release_name,
        "piece length": 16384,
        "length": 1,
        "pieces": sha1(token.encode()).digest(),
    }
    torrent = {
        "announce": build_download_url(
            manager=manager,
            title=title,
            season=season,
            episode=episode,
            year=year,
            source=source,
            manager_id=manager_id,
        ),
        "comment": "AWC rebuild fake torrent handoff",
        "created by": "AnimeWorld Companion",
        "creation date": int(datetime.now(UTC).timestamp()),
        "info": info,
    }
    torrent_name = f"{release_name}.torrent"
    return download, _bencode(torrent), torrent_name


def _download_worker(download_id: str) -> None:
    acquired_slot = False
    entry = get_download(download_id)
    if not entry:
        return
    try:
        _download_semaphore.acquire()
        acquired_slot = True

        url = entry["url"]
        cancel_event = _download_events.setdefault(download_id, threading.Event())
        cancel_event.clear()
        part_path = _localize_data_path(entry.get("part_path") or _part_path(entry["filename"]))
        final_path = _final_path(entry["filename"])
        existing_bytes = os.path.getsize(part_path) if part_path and os.path.exists(part_path) else 0
        headers = {"Range": f"bytes={existing_bytes}-"} if existing_bytes > 0 else {}

        update_download_progress(
            download_id,
            status="downloading",
            started_at=datetime.now(UTC).timestamp(),
            part_path=part_path,
            error="",
            downloaded_bytes=existing_bytes,
            finished_at=None,
        )
        with _download_lock:
            _download_metrics[download_id] = {
                "window_started_at": datetime.now(UTC).timestamp(),
                "window_bytes": 0.0,
                "speed_bps": 0.0,
            }

        with requests.get(url, stream=True, timeout=60, headers=headers) as response:
            if response.status_code == 416 and existing_bytes > 0:
                os.replace(part_path, final_path)
                update_download_progress(
                    download_id,
                    status="completed",
                    downloaded_bytes=existing_bytes,
                    part_path="",
                    finished_at=datetime.now(UTC).timestamp(),
                )
                return

            response.raise_for_status()

            if response.status_code == 206 and existing_bytes > 0:
                total_header = response.headers.get("Content-Range", "").split("/")[-1]
                total = int(total_header) if total_header.isdigit() else 0
                mode = "ab"
                downloaded = existing_bytes
            else:
                total = int(response.headers.get("Content-Length", 0))
                mode = "wb"
                downloaded = 0
                existing_bytes = 0

            update_download_progress(download_id, total_bytes=total, downloaded_bytes=downloaded)
            with open(part_path, mode) as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if cancel_event.is_set():
                        update_download_progress(
                            download_id,
                            status="paused",
                            downloaded_bytes=downloaded,
                            finished_at=None,
                        )
                        return
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    now_ts = datetime.now(UTC).timestamp()
                    with _download_lock:
                        metric = _download_metrics.setdefault(
                            download_id,
                            {"window_started_at": now_ts, "window_bytes": 0.0, "speed_bps": 0.0},
                        )
                        metric["window_bytes"] += float(len(chunk))
                        elapsed_window = now_ts - float(metric["window_started_at"])
                        if elapsed_window >= 0.75:
                            metric["speed_bps"] = metric["window_bytes"] / elapsed_window
                            metric["window_started_at"] = now_ts
                            metric["window_bytes"] = 0.0
                    update_download_progress(download_id, downloaded_bytes=downloaded)
            os.replace(part_path, final_path)
            update_download_progress(
                download_id,
                status="completed",
                downloaded_bytes=downloaded,
                part_path="",
                finished_at=datetime.now(UTC).timestamp(),
            )
    except Exception as exc:
        update_download_progress(
            download_id,
            status="failed",
            error=str(exc),
            finished_at=datetime.now(UTC).timestamp(),
        )
    finally:
        if acquired_slot:
            _download_semaphore.release()
        with _download_lock:
            _download_threads.pop(download_id, None)
            if not get_download(download_id) or get_download(download_id).get("status") not in {"paused", "queued", "resuming", "downloading"}:
                _download_metrics.pop(download_id, None)


def queue_download(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry:
        return None
    if entry["url"].startswith(f"{_download_base_url().rstrip('/')}/api/rebuild/placeholder"):
        return entry
    with _download_lock:
        current = _download_threads.get(download_id)
        if current and current.is_alive():
            return get_download(download_id)
        _download_events[download_id] = threading.Event()
        thread = threading.Thread(target=_download_worker, args=(download_id,), name=f"download-{download_id[:8]}", daemon=True)
        _download_threads[download_id] = thread
        thread.start()
    return get_download(download_id)


def completed_downloads() -> list[dict]:
    return list_completed_downloads()


def mark_imported(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry or entry.get("status") != "completed":
        return None
    return update_download_progress(
        download_id,
        status="imported",
        finished_at=datetime.now(UTC).timestamp(),
    )


def restore_on_startup() -> dict:
    restored = 0
    fixed = 0
    for entry in list_all_downloads():
        restored += 1
        status = entry.get("status", "")
        if status not in {"queued", "downloading"}:
            continue

        part_path = _localize_data_path(entry.get("part_path", ""))
        if part_path and os.path.exists(part_path):
            update_download_progress(
                entry["id"],
                status="paused",
                downloaded_bytes=os.path.getsize(part_path),
                finished_at=None,
                part_path=_localize_data_path(entry.get("part_path", "")),
            )
        else:
            update_download_progress(
                entry["id"],
                status="failed",
                error="Interrupted: server restarted",
                finished_at=datetime.now(UTC).timestamp(),
            )
        fixed += 1
    return {"restored": restored, "fixed": fixed}


def build_download_snapshot(limit: int = 100) -> dict:
    downloads = list_downloads(limit=limit)
    active_statuses = {"queued", "downloading", "resuming", "importing"}
    finished_statuses = {"completed", "imported", "failed", "cancelled", "removed"}
    now = datetime.now(UTC).timestamp()
    enriched = []
    for item in downloads:
        entry = dict(item)
        status = entry.get("status")
        if status not in finished_statuses:
            entry["finished_at"] = None
        total = int(entry.get("total_bytes") or 0)
        downloaded = int(entry.get("downloaded_bytes") or 0)
        started_at = entry.get("started_at") or entry.get("created_at") or now
        finished_at = entry.get("finished_at")
        end_time = finished_at or now
        elapsed = max(float(end_time) - float(started_at), 0.0)
        entry["elapsed"] = round(elapsed, 1)
        entry["percent"] = round((downloaded / total) * 100, 1) if total > 0 else 0.0
        with _download_lock:
            metric = dict(_download_metrics.get(entry["id"], {}))
        entry["speed"] = round(float(metric.get("speed_bps", 0.0)), 1) if status in {"downloading", "resuming"} else 0.0
        part_path = entry.get("part_path") or ""
        entry["resumable"] = status in {"paused", "cancelled", "failed"} and bool(part_path) and os.path.exists(part_path)
        enriched.append(entry)
    return {
        "counts": {
            "total": len(enriched),
            "active": sum(1 for item in enriched if item["status"] in active_statuses),
            "finished": sum(1 for item in enriched if item["status"] in finished_statuses),
        },
        "downloads": enriched,
    }


def cancel_download(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry:
        return None
    event = _download_events.get(download_id)
    if entry.get("status") == "queued":
        return update_download_progress(
            download_id,
            status="cancelled",
            finished_at=datetime.now(UTC).timestamp(),
        )
    if event and entry.get("status") == "downloading":
        event.set()
        return update_download_progress(
            download_id,
            status="paused",
            finished_at=None,
        )
    return entry


def resume_download(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry:
        return None
    part_path = _localize_data_path(entry.get("part_path") or "")
    if not part_path or not os.path.exists(part_path):
        return None
    update_download_progress(
        download_id,
        status="queued",
        error="",
        downloaded_bytes=os.path.getsize(part_path),
        finished_at=None,
    )
    return queue_download(download_id)


def remove_download(download_id: str) -> bool:
    entry = get_download(download_id)
    if not entry:
        return False
    event = _download_events.get(download_id)
    if event:
        event.set()
    _safe_unlink(entry.get("part_path", ""))
    removed = delete_download(download_id)
    with _download_lock:
        _download_threads.pop(download_id, None)
        _download_events.pop(download_id, None)
        _download_metrics.pop(download_id, None)
    return removed


def clear_download_history() -> int:
    return clear_finished_downloads()
