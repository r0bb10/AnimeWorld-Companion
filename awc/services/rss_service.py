"""RSS cache views and maintenance for the clean rebuild."""

from ..repositories.rss_cache import clear_rss_items, list_rss_items


def build_rss_snapshot(limit: int = 100) -> dict:
    items = list_rss_items(limit=limit)
    return {
        "count": len(items),
        "items": items,
    }


def clear_rss_cache() -> dict:
    removed = clear_rss_items()
    return {"removed": removed}
