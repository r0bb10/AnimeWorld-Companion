"""Typed settings for the clean rebuild."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _resolve_database_path() -> str:
    configured = _get("AWC_DATABASE_PATH", "/config/database.db")
    repo_root = Path(__file__).resolve().parents[2]
    repo_candidate = repo_root / "config" / "database.db"
    if repo_candidate.exists():
        return str(repo_candidate)

    if os.path.exists(configured):
        return configured

    return configured


@dataclass(frozen=True)
class Settings:
    aw_base_url: str
    awc_api_key: str
    awc_port: int
    awc_url: str
    log_level: str
    database_path: str
    sync_interval_minutes: int
    sonarr_url: str
    sonarr_api_key: str
    radarr_url: str
    radarr_api_key: str


def load_settings() -> Settings:
    return Settings(
        aw_base_url=_get("AW_BASE_URL"),
        awc_api_key=_get("AWC_API_KEY"),
        awc_port=_int("AWC_PORT", 7004),
        awc_url=_get("AWC_URL"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        database_path=_resolve_database_path(),
        sync_interval_minutes=_int("SYNC_INTERVAL", _int("SONARR_SYNC_INTERVAL", 30)),
        sonarr_url=_get("SONARR_URL"),
        sonarr_api_key=_get("SONARR_API_KEY"),
        radarr_url=_get("RADARR_URL"),
        radarr_api_key=_get("RADARR_API_KEY"),
    )


settings = load_settings()
