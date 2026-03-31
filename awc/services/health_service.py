"""Aggregated health reporting for the clean rebuild."""

from ..integrations.animeworld_client import AnimeWorldClient
from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.sync_meta import get_sync_meta


def build_health_report() -> dict:
    sonarr = SonarrClient()
    radarr = RadarrClient()
    animeworld = AnimeWorldClient()

    sonarr_health = sonarr.health()
    radarr_health = radarr.health()
    animeworld_health = animeworld.health()

    return {
        "sonarr": {
            "ok": sonarr_health.ok,
            "version": sonarr_health.version,
            "error": sonarr_health.error,
            "last_sync": get_sync_meta("last_sonarr_sync"),
        },
        "radarr": {
            "ok": radarr_health.ok,
            "version": radarr_health.version,
            "error": radarr_health.error,
            "last_sync": get_sync_meta("last_radarr_sync"),
        },
        "animeworld": {
            "ok": animeworld_health.ok,
            "url": animeworld_health.url,
        },
    }
