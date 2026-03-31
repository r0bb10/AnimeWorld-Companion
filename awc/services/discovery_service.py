"""AnimeWorld discovery helpers for the clean rebuild."""

from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail


def _dedupe_results(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for item in results:
        key = item.get("url") or item.get("title") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def search_animeworld(query: str, limit: int = 10) -> dict:
    client = AnimeWorldClient()
    return {
        "query": query,
        "search_url": f"{client.base_url}/search?keyword={query.strip()}",
        "results": client.search(query, limit=limit),
    }


def discover_show(show_id: int, limit: int = 10) -> dict | None:
    show = get_show_detail(show_id)
    if not show:
        return None

    client = AnimeWorldClient()
    queries = [show["title"]]
    queries.extend(
        item["title"]
        for item in show.get("alternate_titles", [])
        if item.get("title")
    )
    results = []
    used_queries = []
    for query in queries[:5]:
        if not query or query in used_queries:
            continue
        used_queries.append(query)
        results.extend(client.search(query, limit=limit))
        if len(results) >= limit:
            break

    legacy_links = [
        {
            "link": item.get("url", ""),
            "name": item.get("title", ""),
            "eps": 0,
        }
        for item in _dedupe_results(results)[:limit]
    ]

    return {
        "show_id": show_id,
        "title": show["title"],
        "queries": used_queries,
        "results": _dedupe_results(results)[:limit],
        "links": legacy_links,
    }


def discover_movie(movie_id: int, limit: int = 10) -> dict | None:
    movie = get_movie_detail(movie_id)
    if not movie:
        return None

    client = AnimeWorldClient()
    queries = [movie["title"]]
    queries.extend(
        item["title"]
        for item in movie.get("alternate_titles", [])
        if item.get("title")
    )
    results = []
    used_queries = []
    for query in queries[:5]:
        if not query or query in used_queries:
            continue
        used_queries.append(query)
        results.extend(client.search(query, limit=limit))
        if len(results) >= limit:
            break

    legacy_links = [
        {
            "link": item.get("url", ""),
            "name": item.get("title", ""),
            "eps": 0,
        }
        for item in _dedupe_results(results)[:limit]
    ]

    return {
        "movie_id": movie_id,
        "title": movie["title"],
        "queries": used_queries,
        "results": _dedupe_results(results)[:limit],
        "links": legacy_links,
    }
