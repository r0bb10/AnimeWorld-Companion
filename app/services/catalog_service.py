"""Catalog service for read-only rebuild access to current state."""

from ..repositories.mappings import (
    count_movie_mappings,
    count_show_mappings,
    recent_show_mappings,
)
from ..repositories.movies import count_movies, get_movie_detail, list_movie_summaries
from ..repositories.shows import count_shows, get_show_detail, list_show_summaries


def build_catalog_snapshot(show_limit: int | None = 10, movie_limit: int | None = 10) -> dict:
    return {
        "counts": {
            "shows": count_shows(),
            "movies": count_movies(),
            "show_mappings": count_show_mappings(),
            "movie_mappings": count_movie_mappings(),
        },
        "shows": list_show_summaries(limit=show_limit),
        "movies": list_movie_summaries(limit=movie_limit),
        "recent_show_mappings": recent_show_mappings(),
    }


def build_show_snapshot(show_id: int) -> dict | None:
    return get_show_detail(show_id)


def build_movie_snapshot(movie_id: int) -> dict | None:
    return get_movie_detail(movie_id)
