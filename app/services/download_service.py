"""Fake torrent handoff and background download tracking for the clean rebuild."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha1
import logging
import time
from pathlib import Path
import os
import re
import threading
from urllib.parse import urlencode

import requests

from ..core.config import settings
from ..core.log_events import extract_remote_filename, log_block, log_exception, log_info, log_warning
from ..core.logging import get_logger
from ..domain.media import MediaKind, MediaManager, NamingContext
from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.db import get_db
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
    update_download_status_if_current,
)
from .naming_service import build_release_name

_download_events: dict[str, threading.Event] = {}
_download_threads: dict[str, threading.Thread] = {}
_download_metrics: dict[str, dict[str, float]] = {}
_download_progress: dict[str, dict[str, float | int]] = {}
_download_lock = threading.Lock()
_download_semaphore = threading.Semaphore(max(1, settings.max_concurrent_downloads))
logger = get_logger("download")
_RETRY_BACKOFF_SECONDS = (5, 15, 45, 120)


def _retry_delay_for_attempt(attempt: int) -> int:
    index = max(0, min(len(_RETRY_BACKOFF_SECONDS) - 1, int(attempt) - 1))
    return int(_RETRY_BACKOFF_SECONDS[index])


def _short_error_message(exc: Exception) -> str:
    return " ".join(str(exc).split()).strip() or exc.__class__.__name__


def _retry_error_kind(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        message = _short_error_message(exc).casefold()
        if "name resolution" in message or "nameresolutionerror" in message or "failed to resolve" in message:
            return "dns"
        return "connection"
    if isinstance(exc, requests.ChunkedEncodingError):
        return "incomplete_read"
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        return f"http_{status or 'error'}"
    if isinstance(exc, requests.RequestException):
        return "request"
    return "other"


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.ChunkedEncodingError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {408, 425, 429}:
            return True
        if status >= 500:
            return True
        return False
    return False


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


def _normalize_release_source(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"rss", "search"}:
        return normalized
    return "unknown"


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


def _legacy_query_name(save_name: str) -> str:
    return Path(save_name or "").stem.replace(".", " ").strip()


def resolve_legacy_download_request(*, url: str = "", save_name: str = "", aw_link: str = "") -> dict | None:
    normalized_link = (aw_link or "").strip()
    source = (url or "").strip()
    filename = (save_name or "").strip()
    if not (normalized_link or source or filename):
        return None

    with get_db() as conn:
        if normalized_link:
            row = conn.execute(
                """
                SELECT
                    'sonarr' AS manager,
                    s.title,
                    src.season_number,
                    src.episode_number,
                    NULL AS year,
                    s.sonarr_id AS manager_id
                FROM show_rss_cache src
                JOIN shows s ON s.id = src.show_id
                WHERE src.aw_episode_link = ?
                  AND (? = '' OR src.guid = ?)
                ORDER BY datetime(src.created_at) DESC, src.id DESC
                LIMIT 1
                """,
                (normalized_link, source, source),
            ).fetchone()
            if row:
                resolved = dict(row)
                resolved["source"] = source
                resolved["filename"] = filename
                resolved["aw_link"] = normalized_link
                return resolved

            row = conn.execute(
                """
                SELECT
                    'radarr' AS manager,
                    m.title,
                    NULL AS season_number,
                    NULL AS episode_number,
                    m.year,
                    m.radarr_id AS manager_id
                FROM movie_rss_cache src
                JOIN movies m ON m.id = src.movie_id
                WHERE src.aw_episode_link = ?
                  AND (? = '' OR src.guid = ?)
                ORDER BY datetime(src.created_at) DESC, src.id DESC
                LIMIT 1
                """,
                (normalized_link, source, source),
            ).fetchone()
            if row:
                resolved = dict(row)
                resolved["source"] = source
                resolved["filename"] = filename
                resolved["aw_link"] = normalized_link
                return resolved

            row = conn.execute(
                """
                SELECT
                    'radarr' AS manager,
                    m.title,
                    NULL AS season_number,
                    NULL AS episode_number,
                    m.year,
                    m.radarr_id AS manager_id
                FROM aw_movie_mappings amm
                JOIN movies m ON m.id = amm.movie_id
                WHERE amm.aw_link = ?
                LIMIT 1
                """,
                (normalized_link,),
            ).fetchone()
            if row:
                resolved = dict(row)
                resolved["source"] = source
                resolved["filename"] = filename
                resolved["aw_link"] = normalized_link
                return resolved

            row = conn.execute(
                """
                SELECT
                    'sonarr' AS manager,
                    s.title,
                    s.sonarr_id AS manager_id
                FROM aw_show_mappings asm
                JOIN shows s ON s.id = asm.show_id
                WHERE asm.aw_link = ?
                ORDER BY asm.show_id, asm.season_number
                LIMIT 1
                """,
                (normalized_link,),
            ).fetchone()
            if row and filename:
                from .query_service import parse_query

                parsed = parse_query(_legacy_query_name(filename))
                if parsed.get("season") is not None and parsed.get("episode") is not None:
                    resolved = dict(row)
                    resolved["season_number"] = parsed["season"]
                    resolved["episode_number"] = parsed["episode"]
                    resolved["year"] = None
                    resolved["source"] = source
                    resolved["filename"] = filename
                    resolved["aw_link"] = normalized_link
                    return resolved

    return None


def build_download_url(
    *,
    manager: str,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    source: str = "",
    manager_id: int | None = None,
    aw_link: str = "",
    filename: str | None = None,
    release_source: str = "unknown",
    base_url: str | None = None,
) -> str:
    resolved_source = _decode_source_token(source) or source
    legacy_name = filename or build_release_name(
        _release_context(
            manager=manager,
            title=title,
            season=season,
            episode=episode,
            year=year,
        )
    )
    params: dict[str, str | int] = {}
    if resolved_source:
        params["url"] = resolved_source
    if legacy_name:
        params["save_name"] = legacy_name
    if aw_link:
        params["aw_link"] = aw_link
    if settings.awc_api_key:
        params["apikey"] = settings.awc_api_key
    params["manager"] = manager
    params["title"] = title
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
    params["release_source"] = _normalize_release_source(release_source)
    query = urlencode(params)
    return f"{(base_url or _download_base_url()).rstrip('/')}/download?{query}"


def create_fake_torrent(
    *,
    manager: str,
    title: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    source: str = "",
    manager_id: int | None = None,
    aw_link: str = "",
    filename: str | None = None,
    release_source: str = "unknown",
    base_url: str | None = None,
) -> tuple[dict, bytes, str]:
    release_name = filename or build_release_name(
        _release_context(
            manager=manager,
            title=title,
            season=season,
            episode=episode,
            year=year,
        )
    )
    resolved_source = _decode_source_token(source)
    if not resolved_source:
        # source was empty — no CDN URL available yet; the download worker will
        # have nothing real to fetch.
        log_warning(
            logger,
            "download.queue.no_source",
            "create_fake_torrent: no source URL — download will not start",
            details={"filename": release_name, "manager": manager},
            entity_kind="download",
            entity_id=release_name,
            entity_title=release_name,
        )
        return None, b"", release_name + ".torrent"
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
            release_source=_normalize_release_source(release_source),
            status="queued",
            part_path=_part_path(release_name),
            sonarr_id=manager_id if manager == "sonarr" else None,
            radarr_id=manager_id if manager == "radarr" else None,
            season_number=season if manager == "sonarr" else None,
            episode_number=episode if manager == "sonarr" else None,
        )
        log_block(
            logger,
            logging.INFO,
            f"Queued download: {release_name}",
            [
                f"manager={manager}",
                f"source={resolved_source}",
            ],
            event_type="download.queued",
            entity_kind="download",
            entity_id=download.get("id"),
            entity_title=release_name,
            details={"manager": manager, "source": resolved_source, "release_source": _normalize_release_source(release_source)},
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
            aw_link=aw_link,
            filename=release_name,
            release_source=release_source,
            base_url=base_url,
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
        max_attempts = len(_RETRY_BACKOFF_SECONDS) + 1
        attempt = 0
        cancel_event = _download_events.setdefault(download_id, threading.Event())
        cancel_event.clear()
        while True:
            attempt += 1
            part_path = _localize_data_path(entry.get("part_path") or _part_path(entry["filename"]))
            final_path = _final_path(entry["filename"])
            existing_bytes = os.path.getsize(part_path) if part_path and os.path.exists(part_path) else 0
            headers = {"Range": f"bytes={existing_bytes}-"} if existing_bytes > 0 else {}

            status_value = "resuming" if attempt > 1 else "downloading"
            update_download_progress(
                download_id,
                status=status_value,
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

            try:
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
                    remote_name = extract_remote_filename(response.headers, url)

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

                    log_block(
                        logger,
                        logging.INFO,
                        f"Download started: {entry['filename']}",
                        [
                            f"remote={remote_name or '(unknown)'}",
                            f"resume={'yes' if existing_bytes > 0 else 'no'}",
                        ],
                        event_type="download.started",
                        entity_kind="download",
                        entity_id=download_id,
                        entity_title=entry["filename"],
                        details={"remote": remote_name or "", "resume": bool(existing_bytes > 0)},
                    )

                    progress_interval = 0.5
                    progress_bytes_threshold = 256 * 1024

                    update_download_progress(download_id, total_bytes=total, downloaded_bytes=downloaded)
                    now_ts = datetime.now(UTC).timestamp()
                    with _download_lock:
                        _download_progress[download_id] = {
                            "downloaded_bytes": downloaded,
                            "last_checkpoint_at": now_ts,
                            "last_checkpoint_bytes": downloaded,
                        }
                    with open(part_path, mode) as handle:
                        for chunk in response.iter_content(chunk_size=65536):
                            if cancel_event.is_set():
                                update_download_progress(
                                    download_id,
                                    status="paused",
                                    downloaded_bytes=downloaded,
                                    finished_at=None,
                                )
                                with _download_lock:
                                    _download_progress.pop(download_id, None)
                                log_block(
                                    logger,
                                    logging.INFO,
                                    f"Download paused: {entry['filename']}",
                                    [f"downloaded={downloaded} bytes"],
                                    event_type="download.paused",
                                    entity_kind="download",
                                    entity_id=download_id,
                                    entity_title=entry["filename"],
                                    details={"downloaded_bytes": downloaded},
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
                            with _download_lock:
                                progress_state = _download_progress.setdefault(download_id, {})
                                progress_state["downloaded_bytes"] = downloaded
                                if (
                                    downloaded - progress_state.get("last_checkpoint_bytes", 0) >= progress_bytes_threshold
                                    or now_ts - progress_state.get("last_checkpoint_at", 0) >= progress_interval
                                ):
                                    update_download_progress(download_id, downloaded_bytes=downloaded)
                                    progress_state["last_checkpoint_at"] = now_ts
                                    progress_state["last_checkpoint_bytes"] = downloaded
                    os.replace(part_path, final_path)
                    update_download_progress(
                        download_id,
                        status="completed",
                        downloaded_bytes=downloaded,
                        part_path="",
                        finished_at=datetime.now(UTC).timestamp(),
                    )
                    with _download_lock:
                        _download_progress.pop(download_id, None)
                    log_block(
                        logger,
                        logging.INFO,
                        f"Download completed: {entry['filename']}",
                        [
                            f"saved={final_path}",
                            f"remote={remote_name or '(unknown)'}",
                        ],
                        event_type="download.completed",
                        entity_kind="download",
                        entity_id=download_id,
                        entity_title=entry["filename"],
                        details={"saved": final_path, "remote": remote_name or ""},
                    )
                    return
            except Exception as exc:
                retryable = _is_retryable_download_error(exc)
                exhausted = attempt >= max_attempts
                if retryable and not exhausted:
                    delay = _retry_delay_for_attempt(attempt)
                    kind = _retry_error_kind(exc)
                    error_message = _short_error_message(exc)
                    log_warning(
                        logger,
                        "download.retry.scheduled",
                        "Download retry scheduled",
                        lines=[
                            f"attempt={attempt + 1}/{max_attempts}",
                            f"delay={delay}s",
                            f"kind={kind}",
                            f"error={error_message}",
                        ],
                        details={
                            "filename": entry["filename"],
                            "download_id": download_id,
                            "attempt": attempt + 1,
                            "max_attempts": max_attempts,
                            "delay_seconds": delay,
                            "kind": kind,
                            "error": str(exc),
                        },
                        entity_kind="download",
                        entity_id=download_id,
                        entity_title=entry["filename"],
                    )
                    update_download_progress(
                        download_id,
                        status="resuming",
                        error=f"retrying in {delay}s ({kind})",
                        finished_at=None,
                    )
                    if cancel_event.wait(delay):
                        return
                    continue

                update_download_progress(
                    download_id,
                    status="failed",
                    error=str(exc),
                    finished_at=datetime.now(UTC).timestamp(),
                )
                _download_progress.pop(download_id, None)

                if retryable and exhausted:
                    kind = _retry_error_kind(exc)
                    log_warning(
                        logger,
                        "download.retry.exhausted",
                        "Download failed after retries",
                        lines=[
                            f"attempts={attempt}/{max_attempts}",
                            f"kind={kind}",
                            f"error={_short_error_message(exc)}",
                        ],
                        details={
                            "filename": entry["filename"],
                            "download_id": download_id,
                            "attempts": attempt,
                            "max_attempts": max_attempts,
                            "kind": kind,
                            "error": str(exc),
                        },
                        entity_kind="download",
                        entity_id=download_id,
                        entity_title=entry["filename"],
                    )
                else:
                    log_exception(
                        logger,
                        "download.failed",
                        "Download failed",
                        details={"filename": entry["filename"], "download_id": download_id, "error": str(exc)},
                        entity_kind="download",
                        entity_id=download_id,
                        entity_title=entry["filename"],
                    )
                return
    except Exception as exc:
        update_download_progress(
            download_id,
            status="failed",
            error=str(exc),
            finished_at=datetime.now(UTC).timestamp(),
        )
        _download_progress.pop(download_id, None)
        log_exception(
            logger,
            "download.failed",
            "Download failed",
            details={"filename": entry["filename"], "download_id": download_id, "error": str(exc)},
            entity_kind="download",
            entity_id=download_id,
            entity_title=entry["filename"],
        )
    finally:
        if acquired_slot:
            _download_semaphore.release()
        with _download_lock:
            _download_threads.pop(download_id, None)
            _download_events.pop(download_id, None)
            _download_progress.pop(download_id, None)
            if not get_download(download_id) or get_download(download_id).get("status") not in {"paused", "queued", "resuming", "downloading"}:
                _download_metrics.pop(download_id, None)


def queue_download(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry:
        return None
    with _download_lock:
        current = _download_threads.get(download_id)
        if current and current.is_alive():
            return get_download(download_id)
        _download_events[download_id] = threading.Event()
        thread = threading.Thread(target=_download_worker, args=(download_id,), name=f"download-{download_id[:8]}", daemon=True)
        _download_threads[download_id] = thread
        thread.start()
    return get_download(download_id)


def pause_download(download_id: str, *, reason: str = "user") -> dict | None:
    entry = get_download(download_id)
    if not entry or entry.get("status") != "downloading":
        return entry

    event = _download_events.get(download_id)
    if event:
        event.set()

    updated = update_download_progress(
        download_id,
        status="paused",
        pause_reason=reason,
        finished_at=None,
    )
    if updated:
        log_info(
            logger,
            "download.pause_requested",
            "Download pause requested",
            entity_kind="download",
            entity_id=download_id,
            entity_title=updated.get("filename"),
            lines=[f"reason={reason}"],
            details={"filename": updated.get("filename"), "reason": reason},
        )
    return updated


def pause_active_downloads(reason: str = "system", timeout_seconds: int = 30) -> int:
    active = [entry for entry in list_all_downloads() if entry.get("status") == "downloading"]
    requested = {entry["id"] for entry in active if pause_download(entry["id"], reason=reason)}
    deadline = time.monotonic() + timeout_seconds
    while requested and time.monotonic() < deadline:
        time.sleep(0.1)
        requested = {
            download_id
            for download_id in requested
            if get_download(download_id).get("status") == "downloading"
        }
    return len(active)


def completed_downloads() -> list[dict]:
    return list_completed_downloads()


def _download_basename(value: str) -> str:
    return Path(str(value or "")).name.casefold()


def _sonarr_episode_key(filename: str) -> tuple[int, int] | None:
    match = re.search(r"[Ss](\d+)[Ee](\d+)", filename or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _completed_download_candidates(manager: str, manager_entity_id: int) -> list[dict]:
    entries = [
        entry
        for entry in list_all_downloads()
        if entry.get("status") in {"completed", "vanished"}
    ]
    if manager == "sonarr":
        return [entry for entry in entries if int(entry.get("sonarr_id") or 0) == manager_entity_id]
    if manager == "radarr":
        return [entry for entry in entries if int(entry.get("radarr_id") or 0) == manager_entity_id]
    return []


def _import_source_names(manager: str, payload: dict) -> set[str]:
    names: set[str] = set()

    def _collect(path: str) -> None:
        name = _download_basename(path)
        if name:
            names.add(name)

    _collect(str(payload.get("sourcePath") or ""))

    if manager == "sonarr":
        episode_file = payload.get("episodeFile") or {}
        _collect(str(episode_file.get("sourcePath") or ""))
        _collect(str(episode_file.get("path") or ""))
        for item in payload.get("episodeFiles") or []:
            file_payload = item or {}
            _collect(str(file_payload.get("sourcePath") or ""))
            _collect(str(file_payload.get("path") or ""))
    elif manager == "radarr":
        movie_file = payload.get("movieFile") or {}
        _collect(str(movie_file.get("sourcePath") or ""))
        _collect(str(movie_file.get("path") or ""))
        for item in payload.get("movieFiles") or []:
            file_payload = item or {}
            _collect(str(file_payload.get("sourcePath") or ""))
            _collect(str(file_payload.get("path") or ""))

    return names


def _sonarr_episode_keys(payload: dict) -> set[tuple[int, int]]:
    keys: set[tuple[int, int]] = set()
    for episode in payload.get("episodes") or []:
        try:
            season_number = int(episode.get("seasonNumber"))
            episode_number = int(episode.get("episodeNumber"))
        except (TypeError, ValueError):
            continue
        keys.add((season_number, episode_number))
    return keys


def _unique_match(entries: list[dict], predicate) -> dict | None:
    matches = [entry for entry in entries if predicate(entry)]
    if len(matches) == 1:
        return matches[0]
    return None


def find_completed_download_for_import_webhook(manager: str, manager_entity_id: int, payload: dict) -> dict | None:
    candidates = _completed_download_candidates(manager, manager_entity_id)
    if not candidates:
        return None

    source_names = _import_source_names(manager, payload)
    if source_names:
        match = _unique_match(candidates, lambda entry: _download_basename(entry.get("filename") or "") in source_names)
        if match:
            return match

    if manager == "sonarr":
        episode_keys = _sonarr_episode_keys(payload)
        if episode_keys:
            match = _unique_match(candidates, lambda entry: _sonarr_episode_key(str(entry.get("filename") or "")) in episode_keys)
            if match:
                return match

    return None


def mark_imported(download_id: str, *, emit_log: bool = True) -> dict | None:
    updated = update_download_status_if_current(
        download_id,
        status="imported",
        current_statuses=("completed", "vanished"),
        finished_at=datetime.now(UTC).timestamp(),
    )
    if updated and emit_log:
        log_info(logger, "download.imported", "Download imported", entity_kind="download", entity_id=download_id, entity_title=updated.get("filename"), details={"filename": updated.get("filename")})
    return updated


def mark_vanished(download_id: str, *, emit_log: bool = True) -> dict | None:
    updated = update_download_status_if_current(
        download_id,
        status="vanished",
        current_statuses=("completed",),
    )
    if updated and emit_log:
        log_info(
            logger,
            "download.vanished",
            "Download vanished",
            entity_kind="download",
            entity_id=download_id,
            entity_title=updated.get("filename"),
            details={"filename": updated.get("filename")},
        )
    return updated


def _manager_imported(entry: dict) -> bool | None:
    sonarr_id = entry.get("sonarr_id")
    if sonarr_id is not None:
        season_number = entry.get("season_number")
        episode_number = entry.get("episode_number")
        if season_number is None or episode_number is None:
            parsed_episode = _sonarr_episode_key(str(entry.get("filename") or ""))
            if not parsed_episode:
                return None
            season_number, episode_number = parsed_episode
        return SonarrClient().has_episode_file(
            int(sonarr_id),
            int(season_number),
            int(episode_number),
        )

    radarr_id = entry.get("radarr_id")
    if radarr_id is not None:
        return RadarrClient().has_movie_file(int(radarr_id))
    return None


def reconcile_vanished_downloads() -> dict:
    grace_seconds = 60
    now = datetime.now(UTC).timestamp()
    checked = 0
    imported = 0
    vanished = 0
    skipped = 0

    for entry in list_completed_downloads():
        checked += 1
        filename = str(entry.get("filename") or "").strip()
        if not filename:
            continue
        finished_at = float(entry.get("finished_at") or 0)
        if not finished_at or (now - finished_at) < grace_seconds:
            continue

        final_path = _final_path(filename)
        if final_path and os.path.exists(final_path):
            continue

        manager_imported = _manager_imported(entry)
        if manager_imported is True:
            updated = mark_imported(str(entry.get("id") or ""), emit_log=False)
            if updated:
                imported += 1
            continue
        if manager_imported is None:
            skipped += 1
            continue

        updated = mark_vanished(str(entry.get("id") or ""))
        if updated:
            vanished += 1

    return {
        "checked": checked,
        "imported": imported,
        "vanished": vanished,
        "skipped": skipped,
        "grace_seconds": grace_seconds,
    }


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
                pause_reason="system",
                downloaded_bytes=os.path.getsize(part_path),
                finished_at=None,
                part_path=part_path,
            )
        else:
            update_download_progress(
                entry["id"],
                status="failed",
                error="Interrupted: server restarted",
                finished_at=datetime.now(UTC).timestamp(),
            )
        fixed += 1

    resumed = 0
    retried = 0
    for entry in list_all_downloads():
        status = entry.get("status")
        if status == "paused" and entry.get("pause_reason") == "system":
            part_path = _localize_data_path(entry.get("part_path", ""))
            if part_path and os.path.exists(part_path):
                if resume_download(entry["id"]):
                    resumed += 1
        elif status == "failed":
            if resume_download(entry["id"]):
                retried += 1

    return {"restored": restored, "fixed": fixed, "resumed": resumed, "retried": retried}


def build_download_snapshot(limit: int = 100) -> dict:
    downloads = list_downloads(limit=limit)
    active_statuses = {"queued", "downloading", "resuming", "importing"}
    finished_statuses = {"completed", "imported", "failed", "cancelled", "removed", "vanished"}
    now = datetime.now(UTC).timestamp()
    enriched = []
    for item in downloads:
        entry = dict(item)
        status = entry.get("status")
        if status not in finished_statuses:
            entry["finished_at"] = None
        total = int(entry.get("total_bytes") or 0)
        downloaded = int(entry.get("downloaded_bytes") or 0)
        if status in {"downloading", "resuming"}:
            with _download_lock:
                current_progress = _download_progress.get(entry["id"])
            if current_progress is not None:
                downloaded = int(current_progress.get("downloaded_bytes", downloaded) or downloaded)
                entry["downloaded_bytes"] = downloaded
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
    if entry.get("status") == "queued":
        updated = update_download_progress(
            download_id,
            status="cancelled",
            finished_at=datetime.now(UTC).timestamp(),
        )
        if updated:
            log_info(logger, "download.cancelled", "Download cancelled", entity_kind="download", entity_id=download_id, entity_title=updated.get("filename"), details={"filename": updated.get("filename")})
        return updated
    return pause_download(download_id, reason="user")


def resume_download(download_id: str) -> dict | None:
    entry = get_download(download_id)
    if not entry:
        return None
    status = entry.get("status")
    part_path = _localize_data_path(entry.get("part_path") or "")

    if status == "paused":
        if not part_path or not os.path.exists(part_path):
            return None
        update_download_progress(
            download_id,
            status="queued",
            pause_reason="none",
            error="",
            downloaded_bytes=os.path.getsize(part_path),
            finished_at=None,
        )
        log_info(logger, "download.resumed", "Download resumed", entity_kind="download", entity_id=download_id, entity_title=entry.get("filename"), details={"filename": entry.get("filename")})
        return queue_download(download_id)

    if status == "failed":
        if part_path and os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        update_download_progress(
            download_id,
            status="queued",
            pause_reason="none",
            error="",
            downloaded_bytes=0,
            finished_at=None,
        )
        log_info(logger, "download.resumed", "Download resumed", entity_kind="download", entity_id=download_id, entity_title=entry.get("filename"), details={"filename": entry.get("filename")})
        return queue_download(download_id)

    return None


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
