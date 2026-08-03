"""Shared manager client abstractions for Sonarr and Radarr."""

from dataclasses import dataclass

from .http import JsonApiClient
from ..domain.media import MediaKind, MediaManager


@dataclass(frozen=True)
class ManagerHealth:
    manager: MediaManager
    ok: bool
    version: str = ""
    error: str = ""


@dataclass(frozen=True)
class ManagerProfile:
    manager: MediaManager
    media_kind: MediaKind
    base_url: str
    configured: bool


class MediaManagerClient:
    """Shared shape for Sonarr and Radarr clients."""

    def __init__(
        self,
        manager: MediaManager,
        media_kind: MediaKind,
        api: JsonApiClient | None,
        configured: bool,
        base_url: str,
    ):
        self.manager = manager
        self.media_kind = media_kind
        self.api = api
        self.profile = ManagerProfile(
            manager=manager,
            media_kind=media_kind,
            base_url=base_url,
            configured=configured,
        )

    def is_configured(self) -> bool:
        return self.profile.configured and self.api is not None

    def health(self) -> ManagerHealth:
        api = self.api
        if not self.is_configured() or api is None:
            return ManagerHealth(manager=self.manager, ok=False, error="not configured")
        payload = api.get("system/status")
        if not payload:
            return ManagerHealth(manager=self.manager, ok=False, error="unreachable")
        version = payload.get("version", "") if isinstance(payload, dict) else ""
        return ManagerHealth(manager=self.manager, ok=True, version=version)

    def fetch_tags(self) -> dict[int, str]:
        api = self.api
        if not self.is_configured() or api is None:
            return {}
        payload = api.get("tag")
        if not isinstance(payload, list):
            return {}
        return {
            item["id"]: item["label"]
            for item in payload
            if "id" in item and "label" in item
        }

    def fetch_wanted_missing(self) -> list[dict]:
        api = self.api
        if not self.is_configured() or api is None:
            return []
        payload = api.get("wanted/missing")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            records = payload.get("records")
            if isinstance(records, list):
                return records
        return []
