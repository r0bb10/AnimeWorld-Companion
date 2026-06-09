"""AnimeWorld search helpers."""

from ..integrations.animeworld_client import AnimeWorldClient


def search_animeworld(query: str, limit: int = 10) -> dict:
    client = AnimeWorldClient()
    return {
        "query": query,
        "search_url": f"{client.base_url}/search?keyword={query.strip()}",
        "results": client.search(query, limit=limit),
    }
