"""Unified manager inspection for Sonarr and Radarr."""

from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient


def _manager_snapshot(name: str, client) -> dict:
    health = client.health()
    naming = client.naming_config() if client.is_configured() else {}
    tags = client.fetch_tags() if client.is_configured() else {}

    return {
        "manager": name,
        "configured": client.is_configured(),
        "base_url": client.profile.base_url,
        "ok": health.ok,
        "version": health.version,
        "error": health.error,
        "naming": naming,
        "tags": tags,
    }


def build_manager_snapshot() -> dict:
    sonarr = SonarrClient()
    radarr = RadarrClient()
    return {
        "sonarr": _manager_snapshot("sonarr", sonarr),
        "radarr": _manager_snapshot("radarr", radarr),
    }
