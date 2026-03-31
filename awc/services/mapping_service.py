"""Mapping-domain read logic for the clean rebuild."""

from ..repositories.mappings import (
    get_episode_by_absolute,
    get_internal_episode,
    get_mapping_scenario,
    list_show_mappings,
)
from ..repositories.shows import get_show_detail


def build_show_mapping_snapshot(show_id: int) -> dict | None:
    show = get_show_detail(show_id)
    if not show:
        return None

    seasons = []
    for season in show.get("seasons", []):
        mappings = list_show_mappings(show_id, season["season_number"])
        scenario = "unmapped"
        if mappings:
            scenario = get_mapping_scenario(show_id, mappings[0]["aw_link"])
            if len(mappings) > 1:
                scenario = "split_cour"
            elif mappings[0].get("linked_with_season") is not None:
                scenario = "linked_season"
            else:
                scenario = "direct"

        seasons.append(
            {
                "season_number": season["season_number"],
                "episode_count": season.get("episode_count"),
                "air_date_start": season.get("air_date_start"),
                "air_date_end": season.get("air_date_end"),
                "scenario": scenario,
                "mapping_count": len(mappings),
                "mappings": mappings,
            }
        )

    return {
        "show_id": show["id"],
        "title": show["title"],
        "seasons": seasons,
    }


def resolve_scene_episode(show_id: int, season_number: int, episode_number: int) -> dict:
    resolved = get_internal_episode(show_id, season_number, episode_number)
    if not resolved:
        return {
            "input": {"season_number": season_number, "episode_number": episode_number},
            "resolved": None,
            "matched": False,
        }

    return {
        "input": {"season_number": season_number, "episode_number": episode_number},
        "resolved": {
            "season_number": resolved[0],
            "episode_number": resolved[1],
        },
        "matched": True,
    }


def resolve_absolute_episode(show_id: int, absolute_episode: int) -> dict:
    resolved = get_episode_by_absolute(show_id, absolute_episode)
    if not resolved:
        return {
            "input": {"absolute_episode": absolute_episode},
            "resolved": None,
            "matched": False,
        }

    return {
        "input": {"absolute_episode": absolute_episode},
        "resolved": {
            "season_number": resolved[0],
            "episode_number": resolved[1],
        },
        "matched": True,
    }
