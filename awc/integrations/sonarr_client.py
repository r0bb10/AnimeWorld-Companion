"""Sonarr client for the clean rebuild."""

from .http import JsonApiClient, JsonApiConfig
from .manager_client import MediaManagerClient
from ..core.config import settings
from ..domain.media import MediaKind, MediaManager


class SonarrClient(MediaManagerClient):
    def __init__(self):
        configured = bool(settings.sonarr_url and settings.sonarr_api_key)
        api = None
        if configured:
            api = JsonApiClient(
                JsonApiConfig(
                    base_url=f"{settings.sonarr_url.rstrip('/')}/api/v3",
                    api_key=settings.sonarr_api_key,
                )
            )
        super().__init__(
            manager=MediaManager.SONARR,
            media_kind=MediaKind.SERIES,
            api=api,
            configured=configured,
            base_url=settings.sonarr_url,
        )

    def naming_config(self) -> dict:
        if not self.is_configured():
            return {}
        payload = self.api.get("config/naming")
        return payload if isinstance(payload, dict) else {}

    def fetch_series(self) -> list[dict]:
        if not self.is_configured():
            return []
        payload = self.api.get("series")
        return payload if isinstance(payload, list) else []
