"""Naming rules split by media manager and media kind."""

import re
import time

from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..domain.media import MediaKind, MediaManager, NamingContext

_NAMING_CACHE_TTL = 3600  # 1 hour
_sonarr_naming_cache: dict | None = None
_sonarr_naming_cache_at: float = 0.0
_radarr_naming_cache: dict | None = None
_radarr_naming_cache_at: float = 0.0


def _sonarr_naming_config() -> dict:
    global _sonarr_naming_cache, _sonarr_naming_cache_at
    now = time.monotonic()
    if _sonarr_naming_cache is not None and now - _sonarr_naming_cache_at < _NAMING_CACHE_TTL:
        return _sonarr_naming_cache
    _sonarr_naming_cache = SonarrClient().naming_config()
    _sonarr_naming_cache_at = now
    return _sonarr_naming_cache


def _radarr_naming_config() -> dict:
    global _radarr_naming_cache, _radarr_naming_cache_at
    now = time.monotonic()
    if _radarr_naming_cache is not None and now - _radarr_naming_cache_at < _NAMING_CACHE_TTL:
        return _radarr_naming_cache
    _radarr_naming_cache = RadarrClient().naming_config()
    _radarr_naming_cache_at = now
    return _radarr_naming_cache


def _apply_colon_replacement(title: str, colon_format: int | str) -> str:
    if isinstance(colon_format, str):
        value = colon_format.strip().lower()
        if value == "smart":
            return title.replace(": ", " ").replace(":", "")
        if value == "dash":
            return title.replace(":", "-")
        if value == "space-dash":
            return title.replace(":", " - ")
    if colon_format == 0:
        return title.replace(":", "")
    if colon_format == 1:
        return title.replace(":", "-")
    if colon_format == 2:
        return title.replace(":", " -")
    if colon_format == 3:
        return title.replace(":", " - ")
    return title.replace(": ", " ").replace(":", "")


def _cleanup_filename(value: str) -> str:
    cleaned = re.sub(r"\{[^{}]+\}", "", value)
    cleaned = re.sub(r"\s+", ".", cleaned.strip())
    cleaned = re.sub(r"\.+", ".", cleaned)
    cleaned = cleaned.strip(". -_")
    return cleaned


def _format_series_name(context: NamingContext) -> str:
    title = context.title.strip()
    season = context.season_number or 1
    episode = context.episode_number or 1

    naming_config = _sonarr_naming_config()
    format_string = naming_config.get("animeEpisodeFormat", "{Series.Title}.S{season:00}E{episode:00}")
    colon_format = naming_config.get("colonReplacementFormat", 4)

    title_cleaned = _apply_colon_replacement(title, colon_format)
    title_with_dots = title_cleaned.replace(" ", ".")

    result = re.sub(r"\{Series\.Title\}", title_with_dots, format_string, flags=re.IGNORECASE)
    result = re.sub(r"\{Series\s+Title\}", title_cleaned, result, flags=re.IGNORECASE)
    result = re.sub(
        r"\{season:(\d+)\}",
        lambda match: f"{season:0{len(match.group(1))}d}",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(r"\{season\}", str(season), result, flags=re.IGNORECASE)
    result = re.sub(
        r"\{episode:(\d+)\}",
        lambda match: f"{episode:0{len(match.group(1))}d}",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(r"\{episode\}", str(episode), result, flags=re.IGNORECASE)

    result = _cleanup_filename(result)
    if not result.endswith(".mp4"):
        result += ".mp4"
    return result


def _format_movie_name(context: NamingContext) -> str:
    title = context.title.strip()
    naming_config = _radarr_naming_config()
    format_string = naming_config.get("standardMovieFormat", "{Movie Title}.{Release Year}.WEBDL")
    colon_format = naming_config.get("colonReplacementFormat", 4)

    title_cleaned = _apply_colon_replacement(title, colon_format)
    title_with_dots = title_cleaned.replace(" ", ".")
    year = context.year or ""
    imdb_id = (context.imdb_id or "").strip()

    replacements = {
        r"\{Movie\.CleanTitle\}": title_with_dots,
        r"\{Movie\.Title\}": title_with_dots,
        r"\{Movie\s+Title\}": title_cleaned,
        r"\{\(Release\s+Year\)\}": str(year),
        r"\{Release\.Year\}": str(year),
        r"\{Release\s+Year\}": str(year),
        r"\{Year\}": str(year),
        r"\{ImdbId\}": imdb_id,
    }
    result = format_string
    for pattern, replacement in replacements.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    result = _cleanup_filename(result)
    if not result.endswith(".mp4"):
        result += ".mp4"
    return result


def build_release_name(context: NamingContext) -> str:
    title = context.title.strip()

    if context.manager is MediaManager.RADARR or context.kind is MediaKind.MOVIE:
        return _format_movie_name(context)

    return _format_series_name(context)
