"""Manual mutation workflows for the clean rebuild."""

from ..repositories.mappings import (
    remove_movie_mapping,
    remove_show_mapping,
    replace_movie_mapping,
    replace_show_mapping,
)
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail


def map_show_season(
    *,
    show_id: int,
    season_number: int,
    aw_link: str,
    aw_title: str = "",
    part: int = 1,
    aw_episode_count: int = 0,
    aw_total_episodes: int = 0,
    aw_status: str = "",
    aw_category: str = "",
    linked_with_season: int | None = None,
) -> dict:
    show = get_show_detail(show_id)
    if not show:
        return {"updated": False, "reason": "show_not_found"}
    mappings = replace_show_mapping(
        show_id=show_id,
        season_number=season_number,
        aw_link=aw_link,
        aw_title=aw_title,
        part=part,
        aw_episode_count=aw_episode_count,
        aw_total_episodes=aw_total_episodes,
        aw_status=aw_status,
        aw_category=aw_category,
        linked_with_season=linked_with_season,
    )
    return {
        "updated": True,
        "show_id": show_id,
        "season_number": season_number,
        "mappings": mappings,
    }


def unmap_show_season(show_id: int, season_number: int) -> dict:
    removed = remove_show_mapping(show_id, season_number)
    return {
        "updated": removed > 0,
        "removed": removed,
        "show_id": show_id,
        "season_number": season_number,
    }


def map_movie(
    *,
    movie_id: int,
    aw_link: str,
    aw_title: str = "",
    aw_status: str = "",
    aw_category: str = "",
) -> dict:
    movie = get_movie_detail(movie_id)
    if not movie:
        return {"updated": False, "reason": "movie_not_found"}
    mapping = replace_movie_mapping(
        movie_id=movie_id,
        aw_link=aw_link,
        aw_title=aw_title,
        aw_status=aw_status,
        aw_category=aw_category,
    )
    return {
        "updated": True,
        "movie_id": movie_id,
        "mapping": mapping,
    }


def unmap_movie(movie_id: int) -> dict:
    removed = remove_movie_mapping(movie_id)
    return {
        "updated": removed > 0,
        "removed": removed,
        "movie_id": movie_id,
    }
