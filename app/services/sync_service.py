"""Sync visibility helpers for the clean rebuild."""

from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..repositories.movies import count_movies
from ..repositories.shows import count_shows
from ..repositories.sync_meta import get_sync_meta


def build_sync_overview() -> dict:
    sonarr = SonarrClient()
    radarr = RadarrClient()

    remote_shows = sonarr.fetch_series() if sonarr.is_configured() else []
    remote_movies = radarr.fetch_movies() if radarr.is_configured() else []

    return {
        "sonarr": {
            "configured": sonarr.is_configured(),
            "remote_count": len(remote_shows),
            "local_count": count_shows(),
            "last_sync": get_sync_meta("last_sonarr_sync"),
        },
        "radarr": {
            "configured": radarr.is_configured(),
            "remote_count": len(remote_movies),
            "local_count": count_movies(),
            "last_sync": get_sync_meta("last_radarr_sync"),
        },
    }
