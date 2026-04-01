from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from ..core.config import settings
from ..repositories.rss_cache import list_rss_items
from .search_service import build_movie_search_items, build_show_search_items

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


def _dummy_items_for_media(media: str, category: str = "") -> list[dict]:
    category_ids = {part.strip() for part in category.split(",") if part.strip()}
    effective_media = media
    if media == "search" and "2000" in category_ids:
        effective_media = "movie"

    if effective_media == "movie":
        return [
            {
                "guid": "aw://test/movie-result",
                "title": "AnimeWorld-Companion.Test.Movie.Result.mp4",
                "categories": ["2000"],
                "size": 1,
            }
        ]

    return [
        {
            "guid": "aw://test/anime-result",
            "title": "AnimeWorld-Companion.Test.Result.mp4",
            "categories": ["5070"],
            "size": 1,
        }
    ]


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


def _add_item(
    channel: Element,
    *,
    guid: str,
    title: str,
    category_id: int,
    download_url: str,
    season: int | None = None,
    episode: int | None = None,
    year: int | None = None,
    size: int | str = 1,
    pub_date: str | None = None,
) -> None:
    item = SubElement(channel, "item")
    SubElement(item, "title").text = title
    link = download_url or _build_download_url(guid, title)
    SubElement(item, "guid").text = guid
    SubElement(item, "link").text = link
    SubElement(item, "comments").text = link
    SubElement(item, "pubDate").text = pub_date or format_datetime(datetime.now(UTC))
    SubElement(item, "category").text = "Anime" if category_id == 5070 else "Movies"
    SubElement(item, "size").text = str(size)
    enclosure = SubElement(item, "enclosure")
    enclosure.set("url", link)
    enclosure.set("length", str(size))
    enclosure.set("type", "application/x-bittorrent")

    _add_attr(item, "category", category_id)
    _add_attr(item, "downloadvolumefactor", 0)
    _add_attr(item, "uploadvolumefactor", 1)

    if category_id == 5070:
        if season is not None:
            _add_attr(item, "season", season)
        if episode is not None:
            _add_attr(item, "episode", episode)
    elif year is not None:
        _add_attr(item, "year", year)


def _build_download_url(guid: str, title: str) -> str:
    base = (settings.awc_url or f"http://localhost:{settings.awc_port}").rstrip("/")
    params = [f"url={quote(guid, safe='')}"]
    if title:
        params.append(f"save_name={quote(title, safe='')}")
    if settings.awc_api_key:
        params.append(f"apikey={quote(settings.awc_api_key, safe='')}")
    return f"{base}/download?{'&'.join(params)}"


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

    if not (query or tvdb_id or tmdb_id or imdb_id):
        for item in list_rss_items(limit=100):
            _add_item(
                channel,
                guid=item["guid"],
                title=item["title"],
                category_id=5070,
                download_url="",
                season=item.get("season_number"),
                episode=item.get("episode_number"),
                size=item.get("size", 0),
                pub_date=item.get("pub_date"),
            )
        if len(channel) == 3:
            for item in _dummy_items_for_media(media, category):
                category_id = 2000 if "2000" in item.get("categories", []) else 5070
                _add_item(
                    channel,
                    guid=item["guid"],
                    title=item["title"],
                    category_id=category_id,
                    download_url="",
                    season=season,
                    episode=episode,
                    size=item.get("size", 1),
                )
        return _xml_bytes(root)

    effective_media = media
    if media == "search":
        category_ids = {part.strip() for part in category.split(",") if part.strip()}
        if "2000" in category_ids:
            effective_media = "movie"
    items = (
        build_movie_search_items(query, tmdb_id=tmdb_id, imdb_id=imdb_id)
        if effective_media == "movie"
        else build_show_search_items(query, season, episode, tvdb_id=tvdb_id)
    )
    if not items:
        items = _dummy_items_for_media(effective_media, category)
    for item in items:
        category_id = 2000 if "2000" in item.get("categories", []) else 5070
        _add_item(
            channel,
            guid=item["guid"],
            title=item["title"],
            category_id=category_id,
            download_url=item.get("download_url", ""),
            season=season,
            episode=episode,
            year=None,
            size=item.get("size", 0),
            pub_date=item.get("pubDate"),
        )
    return _xml_bytes(root)
