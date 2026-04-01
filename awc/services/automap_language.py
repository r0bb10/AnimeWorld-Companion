"""Manager-aware language preference helpers for automap."""

from __future__ import annotations

from ..core.config import settings
from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient


def _match_tag_id(tag_map: dict[int, str], target_label: str) -> int | None:
    for tag_id, label in tag_map.items():
        if label.lower() == target_label.lower():
            return tag_id
    return None


def resolve_show_language_preference(show: dict) -> bool:
    sonarr_id = show.get("sonarr_id")
    if not sonarr_id:
        return False
    client = SonarrClient()
    details = client.fetch_series_detail(int(sonarr_id))
    if not details:
        return False
    dub_tag_id = _match_tag_id(client.fetch_tags(), settings.sonarr_dub_tag)
    return bool(dub_tag_id and dub_tag_id in (details.get("tags") or []))


def resolve_movie_language_preference(movie: dict) -> bool:
    radarr_id = movie.get("radarr_id")
    if not radarr_id:
        return False
    client = RadarrClient()
    details = next((item for item in client.fetch_movies() if int(item.get("id", -1)) == int(radarr_id)), None)
    if not details:
        return False
    dub_tag_id = _match_tag_id(client.fetch_tags(), settings.sonarr_dub_tag)
    return bool(dub_tag_id and dub_tag_id in (details.get("tags") or []))
