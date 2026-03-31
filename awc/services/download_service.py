"""Fake torrent handoff and download tracking for the clean rebuild."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from urllib.parse import urlencode

from ..core.config import settings
from ..domain.media import MediaKind, MediaManager, NamingContext
from ..repositories.downloads import (
    clear_finished_downloads,
    create_download,
    delete_download,
    list_downloads,
    update_download_status,
)
from .naming_service import build_release_name


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
    return settings.awc_url or f"http://localhost:{settings.awc_port}"


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
    download = create_download(
        url=_decode_source_token(source) or build_download_url(
            manager=manager,
            title=title,
            season=season,
            episode=episode,
            year=year,
            manager_id=manager_id,
        ),
        filename=release_name,
        status="queued",
        part_path=_part_path(release_name),
        sonarr_id=manager_id if manager == "sonarr" else None,
    )

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


def build_download_snapshot(limit: int = 100) -> dict:
    downloads = list_downloads(limit=limit)
    active_statuses = {"queued", "downloading", "resuming", "importing"}
    return {
        "counts": {
            "total": len(downloads),
            "active": sum(1 for item in downloads if item["status"] in active_statuses),
            "finished": sum(1 for item in downloads if item["status"] not in active_statuses),
        },
        "downloads": downloads,
    }


def cancel_download(download_id: str) -> dict | None:
    return update_download_status(download_id, "cancelled")


def resume_download(download_id: str) -> dict | None:
    return update_download_status(download_id, "queued")


def remove_download(download_id: str) -> bool:
    return delete_download(download_id)


def clear_download_history() -> int:
    return clear_finished_downloads()
