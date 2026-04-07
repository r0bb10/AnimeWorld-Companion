"""Typed settings for the clean rebuild."""

from dataclasses import dataclass
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = _get(name, default)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _resolve_database_path() -> str:
    configured = _get("AWC_DATABASE_PATH", "/config/database.db")
    repo_root = Path(__file__).resolve().parents[2]
    repo_candidate = repo_root / "config" / "database.db"
    in_container = Path("/.dockerenv").exists()

    if in_container and os.path.exists(configured):
        return configured

    if repo_candidate.exists():
        return str(repo_candidate)

    if os.path.exists(configured):
        return configured

    return configured


def _resolve_logging_database_path() -> str:
    configured = _get("AWC_LOGGING_DB_PATH", "/config/logging.db")
    repo_root = Path(__file__).resolve().parents[2]
    repo_candidate = repo_root / "config" / "logging.db"
    in_container = Path("/.dockerenv").exists()

    if in_container:
        return configured

    if repo_candidate.parent.exists():
        return str(repo_candidate)

    return configured


def _host_resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


def _normalize_manager_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        return ""

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if not hostname or _host_resolves(hostname):
        return value

    in_container = Path("/.dockerenv").exists()
    fallback_host = "host.docker.internal" if in_container else "127.0.0.1"
    if hostname in {"sonarr-dev", "radarr-dev", "localhost"}:
        netloc = fallback_host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth = f"{auth}:{parsed.password}"
            netloc = f"{auth}@{netloc}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    return value


@dataclass(frozen=True)
class Settings:
    aw_base_url: str
    awc_api_key: str
    awc_port: int
    awc_url: str
    log_level: str
    timezone_name: str
    log_db_enabled: bool
    log_db_path: str
    log_db_retention_days: int
    data_path: str
    database_path: str
    sync_interval_minutes: int
    max_concurrent_downloads: int
    download_history_days: int
    unmonitor_imported: bool
    ignore_tags: tuple[str, ...]
    rss_enabled: bool
    rss_poll_interval: int
    rss_cache_retention_days: int
    rss_cache_limit: int
    sanitizer_enabled: bool
    eligible_enabled: bool
    eligible_interval: int
    eligible_lookback_days: int
    automap_confidence_threshold: float
    automap_movie_confidence_threshold: float
    sonarr_url: str
    sonarr_api_key: str
    sonarr_anime_tag: str
    sonarr_dub_tag: str
    radarr_url: str
    radarr_api_key: str
    anime_tag: str


def load_settings() -> Settings:
    return Settings(
        aw_base_url=_get("AW_BASE_URL"),
        awc_api_key=_get("AWC_API_KEY"),
        awc_port=_int("AWC_PORT", 7004),
        awc_url=_get("AWC_URL"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        timezone_name=_get("TZ", "UTC"),
        log_db_enabled=_get("LOG_DB_ENABLED", "true").lower() == "true",
        log_db_path=_resolve_logging_database_path(),
        log_db_retention_days=_int("LOG_DB_RETENTION_DAYS", 30),
        data_path=_get("AWC_DATA_PATH", "/data"),
        database_path=_resolve_database_path(),
        sync_interval_minutes=_int("SYNC_INTERVAL", _int("SONARR_SYNC_INTERVAL", 30)),
        max_concurrent_downloads=_int("MAX_CONCURRENT_DOWNLOADS", 10),
        download_history_days=_int("DOWNLOAD_HISTORY_DAYS", 7),
        unmonitor_imported=_get("UNMONITOR_IMPORTED", "false").lower() == "true",
        ignore_tags=tuple(tag.lower() for tag in _csv("IGNORE_TAG")),
        rss_enabled=_get("RSS_ENABLED", "false").lower() == "true",
        rss_poll_interval=_int("RSS_POLL_INTERVAL", 300),
        rss_cache_retention_days=_int("RSS_CACHE_RETENTION_DAYS", 30),
        rss_cache_limit=_int("RSS_CACHE_LIMIT", 100),
        sanitizer_enabled=_get("SANITIZER_ENABLED", "true").lower() == "true",
        eligible_enabled=_get("ELIGIBLE_ENABLED", "true").lower() == "true",
        eligible_interval=_int("ELIGIBLE_INTERVAL", 21600),
        eligible_lookback_days=_int("ELIGIBLE_LOOKBACK_DAYS", 14),
        automap_confidence_threshold=float(_get("AUTOMAP_CONFIDENCE_THRESHOLD", "85")) / 100,
        automap_movie_confidence_threshold=float(_get("AUTOMAP_MOVIE_CONFIDENCE_THRESHOLD", "75")) / 100,
        sonarr_url=_normalize_manager_url(_get("SONARR_URL")),
        sonarr_api_key=_get("SONARR_API_KEY"),
        sonarr_anime_tag=_get("SONARR_ANIME_TAG", _get("ANIME_TAG", "anime")),
        sonarr_dub_tag=_get("SONARR_DUB_TAG", _get("DUB_TAG", "ita")),
        radarr_url=_normalize_manager_url(_get("RADARR_URL")),
        radarr_api_key=_get("RADARR_API_KEY"),
        anime_tag=_get("ANIME_TAG", _get("SONARR_ANIME_TAG", "anime")),
    )


settings = load_settings()

# Validate required settings — hard-fail on startup like the old app/config.py did.
if not settings.aw_base_url:
    raise RuntimeError(
        "AW_BASE_URL is not set. "
        "If using Docker/Portainer, ensure it is listed under 'environment:' in your stack "
        "(e.g. - AW_BASE_URL=${AW_BASE_URL}). Defining it only in .env is not enough — "
        "it must be explicitly forwarded. Example: AW_BASE_URL=https://www.animeworld.ac"
    )
if not settings.awc_api_key:
    raise RuntimeError("AWC_API_KEY is not set. Generate one with: openssl rand -hex 16")
if not (settings.sonarr_url and settings.sonarr_api_key) and not (settings.radarr_url and settings.radarr_api_key):
    raise RuntimeError(
        "No media manager configured. "
        "Set SONARR_URL + SONARR_API_KEY and/or RADARR_URL + RADARR_API_KEY."
    )
