"""Mapping-aware AnimeWorld search orchestration."""

import logging
from urllib.parse import quote

from ..core.config import settings
from ..core.logging import get_logger
from ..domain.media import MediaKind, MediaManager, NamingContext
from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .download_service import build_download_url
from .mapping_service import resolve_scene_episode
from .naming_service import build_release_name
from .query_service import parse_query

logger = get_logger("search")


def _match_episode(episodes: list[dict], episode_number: int) -> dict | None:
    for episode in episodes:
        try:
            if int(float(episode.get("number", 0))) == int(episode_number):
                return episode
        except Exception:
            continue
    return None


def _series_items(show: dict, season_number: int, episode_number: int) -> list[dict]:
    mappings = []
    for season in show.get("seasons", []):
        if season.get("season_number") == season_number:
            mappings = season.get("mappings", [])
            break
    if not mappings:
        return []

    client = AnimeWorldClient()
    results: list[dict] = []
    for mapping in mappings:
        episodes = client.get_episodes(mapping["aw_link"])
        if not episodes:
            continue

        if len(mappings) > 1:
            cumulative = 0
            target = None
            for part_mapping in sorted(mappings, key=lambda item: item.get("part", 0)):
                part_count = part_mapping.get("aw_episode_count", 0)
                if episode_number <= cumulative + part_count:
                    if part_mapping["aw_link"] == mapping["aw_link"]:
                        target = episode_number - cumulative
                    break
                cumulative += part_count
            if target is None:
                continue
            matched_episode = _match_episode(episodes, target)
        else:
            linked_with = mapping.get("linked_with_season")
            if linked_with is not None:
                offset = 0
                for season in sorted(show.get("seasons", []), key=lambda item: item["season_number"]):
                    current = season["season_number"]
                    if current < linked_with:
                        continue
                    if current >= season_number:
                        break
                    season_mappings = season.get("mappings", [])
                    if season_mappings and season_mappings[0].get("aw_link") == mapping["aw_link"]:
                        offset += season.get("episode_count", 0)
                matched_episode = _match_episode(episodes, offset + episode_number)
            else:
                matched_episode = _match_episode(episodes, episode_number)

        if not matched_episode:
            continue

        file_infos = client.get_file_info(matched_episode.get("episode_id", ""))
        for file_info in file_infos:
            title = build_release_name(
                NamingContext(
                    manager=MediaManager.SONARR,
                    kind=MediaKind.SERIES,
                    title=show["title"],
                    season_number=season_number,
                    episode_number=episode_number,
                )
            )
            source = file_info.get("url", "")
            results.append(
                {
                    "title": title,
                    "guid": source,
                    "size": file_info.get("total_bytes", 0),
                    "categories": ["5070"],
                    "pubDate": file_info.get("last_modified"),
                    "download_url": build_download_url(
                        manager="sonarr",
                        title=show["title"],
                        season=season_number,
                        episode=episode_number,
                        source=source,
                        manager_id=show.get("sonarr_id"),
                        aw_link=mapping["aw_link"],
                        filename=title,
                    ),
                    "aw_link": mapping["aw_link"],
                }
            )
        if results:
            break

    return results


def build_show_search_items(query: str, season_number: int | None, episode_number: int | None, tvdb_id: int | None = None) -> list[dict]:
    from ..repositories.shows import find_show_by_title, find_show_by_tvdb_id

    parsed = parse_query(query)
    parsed_title = parsed.get("title", "")
    if season_number is None:
        season_number = parsed.get("season")
    if episode_number is None:
        episode_number = parsed.get("episode")

    if season_number is None or episode_number is None:
        logger.debug("Show search rejected: missing season/episode for query=%r", query)
        return []

    show = find_show_by_tvdb_id(tvdb_id) if tvdb_id is not None else None
    if not show:
        show = find_show_by_title(query)
    if not show and parsed_title and parsed_title != query:
        show = find_show_by_title(parsed_title)
    if not show:
        logger.debug("Show search miss: query=%r parsed=%r tvdb_id=%r", query, parsed_title, tvdb_id)
        return []

    resolved = resolve_scene_episode(show["id"], season_number, episode_number)
    if resolved.get("matched") and resolved.get("resolved"):
        season_number = resolved["resolved"]["season_number"]
        episode_number = resolved["resolved"]["episode_number"]

    detail = get_show_detail(show["id"])
    if not detail:
        logger.debug("Show search detail miss: show_id=%s query=%r", show["id"], query)
        return []
    items = _series_items(detail, season_number, episode_number)
    logger.debug(
        "Show search resolved: title=%s season=%s episode=%s items=%s",
        detail.get("title"),
        season_number,
        episode_number,
        len(items),
    )
    return items


def build_movie_search_items(query: str, tmdb_id: int | None = None, imdb_id: str = "") -> list[dict]:
    from ..repositories.movies import find_movie_by_external_ids, find_movie_by_title

    parsed = parse_query(query)
    parsed_title = parsed.get("title", "")
    movie = find_movie_by_external_ids(tmdb_id=tmdb_id, imdb_id=imdb_id) if (tmdb_id or imdb_id) else None
    if not movie:
        movie = find_movie_by_title(query)
    if not movie and parsed_title and parsed_title != query:
        movie = find_movie_by_title(parsed_title)
    if not movie:
        logger.debug("Movie search miss: query=%r parsed=%r tmdb_id=%r imdb_id=%r", query, parsed_title, tmdb_id, imdb_id)
        return []

    detail = get_movie_detail(movie["id"])
    if not detail or not detail.get("mapping"):
        logger.debug("Movie search unmapped: title=%s", movie.get("title"))
        return []

    client = AnimeWorldClient()
    episodes = client.get_episodes(detail["mapping"]["aw_link"])
    if not episodes:
        logger.debug("Movie search found no episodes: title=%s aw_link=%s", detail.get("title"), detail["mapping"]["aw_link"])
        return []

    episode = episodes[0]
    file_infos = client.get_file_info(episode.get("episode_id", ""))
    results = []
    for file_info in file_infos:
        title = build_release_name(
            NamingContext(
                manager=MediaManager.RADARR,
                kind=MediaKind.MOVIE,
                title=detail["title"],
                year=detail.get("year"),
                imdb_id=detail.get("imdb_id"),
            )
        )
        source = file_info.get("url", "")
        results.append(
            {
                "title": title,
                "guid": source,
                "size": file_info.get("total_bytes", 0),
                "categories": ["2000"],
                "pubDate": file_info.get("last_modified"),
                "download_url": build_download_url(
                    manager="radarr",
                    title=detail["title"],
                    year=detail.get("year"),
                    source=source,
                    manager_id=detail.get("radarr_id"),
                    aw_link=detail["mapping"]["aw_link"],
                    filename=title,
                ),
                "aw_link": detail["mapping"]["aw_link"],
                "year": detail.get("year"),
            }
        )
    logger.debug("Movie search resolved: title=%s items=%s", detail.get("title"), len(results))
    return results
