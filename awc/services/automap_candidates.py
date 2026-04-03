"""Shared candidate discovery and enrichment for automap."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from ..integrations.animeworld_client import AnimeWorldClient
from .query_helper import build_query_variants
from .automap_scoring import parse_italian_date


def _collect_titles(primary_title: str, alternate_titles: list[dict]) -> list[str]:
    raw_values = [str(primary_title or ""), *[str(item.get("title") or "") for item in alternate_titles]]
    titles: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> bool:
        query = " ".join(str(value or "").split()).strip()
        if not query:
            return False
        key = query.casefold()
        if key in seen:
            return False
        seen.add(key)
        titles.append(query)
        return False

    for value in raw_values:
        variants = build_query_variants(value)
        if variants:
            add(variants[0])

    for value in raw_values:
        for query in build_query_variants(value)[1:]:
            add(query)
    return titles


def _dedupe_by_slug(client: AnimeWorldClient, results: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for item in results:
        slug = client.url_to_slug(item.get("url") or item.get("slug", ""))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        unique.append(item)
    return unique


def _enrich_result(client: AnimeWorldClient, item: dict) -> dict:
    target = item.get("url") or item.get("slug") or ""
    info, episodes, page_url, is_placeholder = client.get_info_and_episodes_meta(target)
    non_special, total, _ = client.count_non_special_episodes(episodes)
    declared_episodes = client.parse_episode_count_value(info.get("Episodi") or info.get("episodes"))
    release_value = str(info.get("Data di Uscita") or info.get("release_date") or "")
    release_dt = parse_italian_date(release_value) if release_value else None
    return {
        **item,
        "aw_link": client.url_to_slug(target),
        "aw_page_url": page_url,
        "aw_title": item.get("title", ""),
        "aw_jtitle": item.get("japanese_title", ""),
        "aw_status": str(info.get("Stato") or info.get("status") or ""),
        "aw_category": str(info.get("Categoria") or info.get("category") or item.get("kind") or ""),
        "aw_audio": str(info.get("Audio") or info.get("audio") or ""),
        "aw_year": release_dt.year if release_dt else None,
        "aw_release_datetime": release_dt,
        "aw_episode_count": declared_episodes if declared_episodes is not None else non_special,
        "aw_total_episodes": total,
        "aw_is_placeholder": is_placeholder,
    }


def discover_candidates_for_titles(primary_title: str, alternate_titles: list[dict], limit: int | None = None) -> list[dict]:
    client = AnimeWorldClient()
    raw_results: list[dict] = []
    for query in _collect_titles(primary_title, alternate_titles):
        raw_results.extend(client.search(query, limit=None))
    unique = _dedupe_by_slug(client, raw_results)
    if limit is not None:
        unique = unique[:limit]

    enriched: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(unique), 6) or 1) as pool:
        futures = {pool.submit(_enrich_result, client, item): item for item in unique}
        for future in as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception:
                continue

    enriched.sort(key=lambda item: item.get("aw_title") or item.get("title") or "")
    return enriched
