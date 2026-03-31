"""Sonarr client for the clean rebuild."""

import requests

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

    def fetch_series_detail(self, series_id: int) -> dict | None:
        if not self.is_configured():
            return None
        payload = self.api.get(f"series/{series_id}")
        return payload if isinstance(payload, dict) else None

    def fetch_episodes(self, series_id: int) -> list[dict]:
        if not self.is_configured():
            return []
        payload = self.api.get(f"episode?seriesId={series_id}")
        return payload if isinstance(payload, list) else []

    def fetch_season_episodes(self, series_id: int, season_number: int) -> list[dict]:
        if not self.is_configured():
            return []
        payload = self.api.get(f"episode?seriesId={series_id}&seasonNumber={season_number}")
        return payload if isinstance(payload, list) else []

    def unmonitor_episode(self, episode_id: int) -> bool:
        if not self.is_configured():
            return False
        url = f"{settings.sonarr_url.rstrip('/')}/api/v3/episode/monitor"
        headers = {"X-Api-Key": settings.sonarr_api_key} if settings.sonarr_api_key else {}
        payload = {"episodeIds": [episode_id], "monitored": False}
        try:
            response = requests.put(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False
