"""AnimeWorld discovery helpers for the clean rebuild."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from ..integrations.animeworld_client import AnimeWorldClient
from ..repositories.movies import get_movie_detail
from ..repositories.shows import get_show_detail
from .query_helper import build_query_variants


def _collect_queries(title: str, alternate_titles: list[dict], limit: int = 24) -> list[str]:
    raw_values = [str(title or ""), *[str(item.get("title") or "") for item in alternate_titles]]
    queries: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> bool:
        query = " ".join(str(value or "").split()).strip()
        if not query:
            return False
        key = query.casefold()
        if key in seen:
            return False
        seen.add(key)
        queries.append(query)
        return len(queries) >= limit

    for value in raw_values:
        variants = build_query_variants(value)
        if variants and add(variants[0]):
            return queries

    for value in raw_values:
        for query in build_query_variants(value)[1:]:
            if add(query):
                return queries

    return queries


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


def _enrich_results(client: AnimeWorldClient, results: list[dict], limit: int) -> tuple[list[dict], list[dict]]:
    unique = _dedupe_results(results)[:limit]

    def enrich(item: dict) -> dict:
        target = item.get("url") or item.get("slug") or ""
        info, episodes = client.get_info_and_episodes(target)
        non_special, total, _ = client.count_non_special_episodes(episodes)
        enriched = dict(item)
        enriched["aw_link"] = client.url_to_slug(target)
        enriched["aw_title"] = item.get("title", "")
        enriched["aw_jtitle"] = item.get("japanese_title", "")
        enriched["aw_status"] = str(info.get("Stato") or info.get("status") or "")
        enriched["aw_category"] = str(info.get("Categoria") or info.get("category") or item.get("kind") or "")
        enriched["aw_audio"] = str(info.get("Audio") or info.get("audio") or "")
        enriched["aw_episode_count"] = non_special
        enriched["aw_total_episodes"] = total
        enriched["eps"] = non_special
        return enriched

    enriched_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(unique), 5) or 1) as pool:
        futures = {pool.submit(enrich, item): item for item in unique}
        for future in as_completed(futures):
            try:
                enriched_results.append(future.result())
            except Exception:
                enriched_results.append(dict(futures[future]))

    enriched_results.sort(key=lambda item: item.get("aw_title") or item.get("title") or "")

    legacy_links = [
        {
            "link": item.get("url", ""),
            "name": item.get("title", ""),
            "eps": int(item.get("aw_episode_count") or item.get("eps") or 0),
            "aw_link": item.get("aw_link", ""),
            "aw_episode_count": int(item.get("aw_episode_count") or 0),
            "aw_total_episodes": int(item.get("aw_total_episodes") or 0),
            "aw_status": item.get("aw_status", ""),
            "aw_category": item.get("aw_category", ""),
            "aw_audio": item.get("aw_audio", ""),
        }
        for item in enriched_results
    ]

    return enriched_results, legacy_links


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
    used_queries = _collect_queries(show["title"], show.get("alternate_titles", []))
    results: list[dict] = []
    for query in used_queries:
        results.extend(client.search(query, limit=limit))

    enriched_results, legacy_links = _enrich_results(client, results, limit)

    return {
        "show_id": show_id,
        "title": show["title"],
        "queries": used_queries,
        "results": enriched_results,
        "links": legacy_links,
    }


def discover_movie(movie_id: int, limit: int = 10) -> dict | None:
    movie = get_movie_detail(movie_id)
    if not movie:
        return None

    client = AnimeWorldClient()
    used_queries = _collect_queries(movie["title"], movie.get("alternate_titles", []))
    results: list[dict] = []
    for query in used_queries:
        results.extend(client.search(query, limit=limit))

    enriched_results, legacy_links = _enrich_results(client, results, limit)

    return {
        "movie_id": movie_id,
        "title": movie["title"],
        "queries": used_queries,
        "results": enriched_results,
        "links": legacy_links,
    }
