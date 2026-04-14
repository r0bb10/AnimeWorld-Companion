"""Shared HTTP client primitives."""

from dataclasses import dataclass
import requests


@dataclass(frozen=True)
class JsonApiConfig:
    base_url: str
    api_key: str = ""
    timeout: int = 15


class JsonApiClient:
    """Small shared client for manager integrations."""

    def __init__(self, config: JsonApiConfig):
        self.config = config

    def get(self, endpoint: str) -> dict | list | None:
        url = f"{self.config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {"X-Api-Key": self.config.api_key} if self.config.api_key else {}
        try:
            response = requests.get(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None
