"""Naming rules split by media manager and media kind."""

import re

from ..integrations.radarr_client import RadarrClient
from ..integrations.sonarr_client import SonarrClient
from ..domain.media import MediaKind, MediaManager, NamingContext


def _apply_colon_replacement(title: str, colon_format: int) -> str:
    if colon_format == 0:
        return title.replace(":", "")
    if colon_format == 1:
        return title.replace(":", "-")
    if colon_format == 2:
        return title.replace(":", " -")
    if colon_format == 3:
        return title.replace(":", " - ")
    return title.replace(": ", " ").replace(":", "")


def _format_series_name(context: NamingContext) -> str:
    title = context.title.strip()
    season = context.season_number or 1
    episode = context.episode_number or 1

    sonarr = SonarrClient()
    naming_config = sonarr.naming_config()
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

    if not result.endswith(".mp4"):
        result += ".mp4"
    return result


def _format_movie_name(context: NamingContext) -> str:
    title = context.title.strip()
    radarr = RadarrClient()
    naming_config = radarr.naming_config()
    format_string = naming_config.get("standardMovieFormat", "{Movie Title}.{Release Year}.WEBDL")
    colon_format = naming_config.get("colonReplacementFormat", 4)

    title_cleaned = _apply_colon_replacement(title, colon_format)
    title_with_dots = title_cleaned.replace(" ", ".")
    year = context.year or ""

    result = re.sub(r"\{Movie\.Title\}", title_with_dots, format_string, flags=re.IGNORECASE)
    result = re.sub(r"\{Movie\s+Title\}", title_cleaned, result, flags=re.IGNORECASE)
    result = re.sub(r"\{Release\.Year\}", str(year), result, flags=re.IGNORECASE)
    result = re.sub(r"\{Release\s+Year\}", str(year), result, flags=re.IGNORECASE)
    result = re.sub(r"\{Year\}", str(year), result, flags=re.IGNORECASE)

    result = re.sub(r"\.+", ".", result).strip(". ")
    if not result.endswith(".mp4"):
        result += ".mp4"
    return result


def build_release_name(context: NamingContext) -> str:
    title = context.title.strip()

    if context.manager is MediaManager.RADARR or context.kind is MediaKind.MOVIE:
        return _format_movie_name(context)

    return _format_series_name(context)
