"""Shared media abstractions for Sonarr and Radarr parity."""

from dataclasses import dataclass
from enum import Enum


class MediaManager(str, Enum):
    SONARR = "sonarr"
    RADARR = "radarr"


class MediaKind(str, Enum):
    SERIES = "series"
    MOVIE = "movie"


@dataclass(frozen=True)
class NamingContext:
    manager: MediaManager
    kind: MediaKind
    title: str
    season_number: int | None = None
    episode_number: int | None = None
    absolute_episode: int | None = None
    year: int | None = None
    imdb_id: str | None = None
