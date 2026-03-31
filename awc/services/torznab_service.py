from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from ..core.config import settings
from ..domain.media import MediaKind, MediaManager, NamingContext
from ..repositories.movies import find_movie_by_external_ids, find_movie_by_title
from ..repositories.shows import find_show_by_title, find_show_by_tvdb_id
from .download_service import build_download_url
from .query_service import parse_query
from .naming_service import build_release_name

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
register_namespace("torznab", TORZNAB_NS)


def _xml_bytes(root: Element) -> bytes:
    return tostring(root, encoding="utf-8", xml_declaration=True)


def build_caps_xml() -> bytes:
    root = Element("caps")

    server = SubElement(root, "server")
    server.set("version", "1.0")
    server.set("title", "AnimeWorld Companion")
    server.set("strapline", "Clean rebuild in progress")
    server.set("email", "noreply@example.invalid")
    server.set("url", settings.awc_url or f"http://localhost:{settings.awc_port}")

    limits = SubElement(root, "limits")
    limits.set("default", "100")
    limits.set("max", "100")

    searching = SubElement(root, "searching")

    search = SubElement(searching, "search")
    search.set("available", "yes")
    search.set("supportedParams", "q")

    tvsearch = SubElement(searching, "tv-search")
    tvsearch.set("available", "yes")
    tvsearch.set("supportedParams", "q,season,ep")

    movie = SubElement(searching, "movie-search")
    movie.set("available", "yes")
    movie.set("supportedParams", "q")

    categories = SubElement(root, "categories")
    anime = SubElement(categories, "category")
    anime.set("id", "5070")
    anime.set("name", "Anime")

    movies = SubElement(categories, "category")
    movies.set("id", "2000")
    movies.set("name", "Movies")

    return _xml_bytes(root)


def _build_rss_root() -> tuple[Element, Element]:
    root = Element("rss")
    root.set("version", "2.0")

    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = "AnimeWorld Companion"
    SubElement(channel, "description").text = "Clean rebuild in progress"
    SubElement(channel, "link").text = settings.awc_url or f"http://localhost:{settings.awc_port}"
    return root, channel


def _add_attr(item: Element, name: str, value: str | int) -> None:
    attr = SubElement(item, f"{{{TORZNAB_NS}}}attr")
    attr.set("name", name)
    attr.set("value", str(value))


def _item_url(item_id: str) -> str:
    base = settings.awc_url or f"http://localhost:{settings.awc_port}"
    return f"{base.rstrip('/')}/api/rebuild/placeholder/{escape(item_id)}"


def _add_item(
    channel: Element,
    *,
    guid: str,
    title: str,
    source_title: str,
    category_id: int,
    kind: MediaKind,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    manager_id: int | None = None,
) -> None:
    item = SubElement(channel, "item")
    SubElement(item, "title").text = title
    link = build_download_url(
        manager="sonarr" if kind is MediaKind.SERIES else "radarr",
        title=source_title,
        season=season,
        episode=episode,
        year=year,
        manager_id=manager_id,
        source=_item_url(guid),
    )
    SubElement(item, "guid").text = guid
    SubElement(item, "link").text = link
    SubElement(item, "comments").text = link
    SubElement(item, "pubDate").text = format_datetime(datetime.now(UTC))
    SubElement(item, "category").text = "Anime" if category_id == 5070 else "Movies"
    SubElement(item, "size").text = "1"

    _add_attr(item, "category", category_id)
    _add_attr(item, "downloadvolumefactor", 0)
    _add_attr(item, "uploadvolumefactor", 1)

    if kind is MediaKind.SERIES:
        if season is not None:
            _add_attr(item, "season", season)
        if episode is not None:
            _add_attr(item, "episode", episode)
    elif year is not None:
        _add_attr(item, "year", year)


def build_search_xml(
    *,
    query: str = "",
    media: str = "search",
    season: int | None = None,
    episode: int | None = None,
    category: str = "",
    tvdb_id: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str = "",
) -> bytes:
    root, channel = _build_rss_root()

    effective_media = media
    if media == "search":
        category_ids = {part.strip() for part in category.split(",") if part.strip()}
        if "2000" in category_ids:
            effective_media = "movie"
        elif "5070" in category_ids:
            effective_media = "show"

    if effective_media == "movie":
        match = find_movie_by_external_ids(tmdb_id=tmdb_id, imdb_id=imdb_id) or find_movie_by_title(query)
        if match:
            title = build_release_name(
                NamingContext(
                    manager=MediaManager.RADARR,
                    kind=MediaKind.MOVIE,
                    title=match["title"],
                    year=match.get("year"),
                )
            )
            _add_item(
                channel,
                guid=f"movie-{match['id']}",
                title=title,
                source_title=match["title"],
                category_id=2000,
                kind=MediaKind.MOVIE,
                year=match.get("year"),
                manager_id=match.get("radarr_id"),
            )
        return _xml_bytes(root)

    parsed = parse_query(query)
    effective_season = season if season is not None else parsed.get("season")
    effective_episode = episode if episode is not None else parsed.get("episode")
    title_query = parsed.get("title") or query

    match = find_show_by_tvdb_id(tvdb_id) or find_show_by_title(title_query)
    if not match:
        return _xml_bytes(root)

    if effective_season is None:
        effective_season = 1
    if effective_episode is None:
        effective_episode = 1

    title = build_release_name(
        NamingContext(
            manager=MediaManager.SONARR,
            kind=MediaKind.SERIES,
            title=match["title"],
            season_number=effective_season,
            episode_number=effective_episode,
        )
    )
    _add_item(
        channel,
        guid=f"show-{match['id']}-s{effective_season}e{effective_episode}",
        title=title,
        source_title=match["title"],
        category_id=5070,
        kind=MediaKind.SERIES,
        season=effective_season,
        episode=effective_episode,
        manager_id=match.get("sonarr_id"),
    )
    return _xml_bytes(root)
