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
        api = self.api
        if not self.is_configured() or api is None:
            return {}
        payload = api.get("config/naming")
        return payload if isinstance(payload, dict) else {}

    def fetch_series(self) -> list[dict]:
        api = self.api
        if not self.is_configured() or api is None:
            return []
        payload = api.get("series")
        return payload if isinstance(payload, list) else []

    def fetch_series_detail(self, series_id: int) -> dict | None:
        api = self.api
        if not self.is_configured() or api is None:
            return None
        payload = api.get(f"series/{series_id}")
        return payload if isinstance(payload, dict) else None

    def fetch_episodes(self, series_id: int) -> list[dict]:
        api = self.api
        if not self.is_configured() or api is None:
            return []
        payload = api.get(f"episode?seriesId={series_id}")
        return payload if isinstance(payload, list) else []

    def has_episode_file(self, series_id: int, season_number: int, episode_number: int) -> bool | None:
        api = self.api
        if not self.is_configured() or api is None:
            return None
        payload = api.get(f"episode?seriesId={series_id}")
        if not isinstance(payload, list):
            return None
        for episode in payload:
            if (
                int(episode.get("seasonNumber") or -1) == season_number
                and int(episode.get("episodeNumber") or -1) == episode_number
            ):
                return bool(episode.get("hasFile"))
        return False

    def unmonitor_episodes(self, episode_ids: list[int]) -> bool:
        if not self.is_configured() or not episode_ids:
            return False
        url = f"{settings.sonarr_url.rstrip('/')}/api/v3/episode/monitor"
        headers = {"X-Api-Key": settings.sonarr_api_key} if settings.sonarr_api_key else {}
        payload = {"episodeIds": episode_ids, "monitored": False}
        try:
            response = requests.put(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def unmonitor_episode(self, episode_id: int) -> bool:
        return self.unmonitor_episodes([episode_id])

    def update_series(self, series_id: int, payload: dict) -> bool:
        if not self.is_configured() or not payload:
            return False
        url = f"{settings.sonarr_url.rstrip('/')}/api/v3/series/{series_id}"
        headers = {"X-Api-Key": settings.sonarr_api_key, "Content-Type": "application/json"}
        try:
            response = requests.put(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False
