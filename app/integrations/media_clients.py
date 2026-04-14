"""First-class Sonarr and Radarr client wrappers."""

from .http import JsonApiClient, JsonApiConfig
from ..core.config import settings
from ..domain.media import MediaManager


def build_media_client(manager: MediaManager) -> JsonApiClient | None:
    if manager is MediaManager.SONARR and settings.sonarr_url and settings.sonarr_api_key:
        return JsonApiClient(
            JsonApiConfig(
                base_url=f"{settings.sonarr_url.rstrip('/')}/api/v3",
                api_key=settings.sonarr_api_key,
            )
        )

    if manager is MediaManager.RADARR and settings.radarr_url and settings.radarr_api_key:
        return JsonApiClient(
            JsonApiConfig(
                base_url=f"{settings.radarr_url.rstrip('/')}/api/v3",
                api_key=settings.radarr_api_key,
            )
        )

    return None
