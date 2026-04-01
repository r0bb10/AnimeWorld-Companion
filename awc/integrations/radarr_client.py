"""Radarr client for the clean rebuild."""

import requests

from .http import JsonApiClient, JsonApiConfig
from .manager_client import MediaManagerClient
from ..core.config import settings
from ..domain.media import MediaKind, MediaManager


class RadarrClient(MediaManagerClient):
    def __init__(self):
        configured = bool(settings.radarr_url and settings.radarr_api_key)
        api = None
        if configured:
            api = JsonApiClient(
                JsonApiConfig(
                    base_url=f"{settings.radarr_url.rstrip('/')}/api/v3",
                    api_key=settings.radarr_api_key,
                )
            )
        super().__init__(
            manager=MediaManager.RADARR,
            media_kind=MediaKind.MOVIE,
            api=api,
            configured=configured,
            base_url=settings.radarr_url,
        )

    def fetch_movies(self) -> list[dict]:
        if not self.is_configured():
            return []
        payload = self.api.get("movie")
        return payload if isinstance(payload, list) else []

    def naming_config(self) -> dict:
        if not self.is_configured():
            return {}
        payload = self.api.get("config/naming")
        return payload if isinstance(payload, dict) else {}

    def fetch_movie_detail(self, movie_id: int) -> dict | None:
        if not self.is_configured():
            return None
        payload = self.api.get(f"movie/{movie_id}")
        return payload if isinstance(payload, dict) else None

    def unmonitor_movie(self, movie_id: int) -> bool:
        if not self.is_configured():
            return False
        url = f"{settings.radarr_url.rstrip('/')}/api/v3/movie/editor"
        headers = {"X-Api-Key": settings.radarr_api_key} if settings.radarr_api_key else {}
        payload = {"movieIds": [movie_id], "monitored": False}
        try:
            response = requests.put(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False
