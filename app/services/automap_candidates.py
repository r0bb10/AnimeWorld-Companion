"""Shared candidate discovery and enrichment for automap."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import re

from ..integrations.animeworld_client import AnimeWorldClient
from .query_helper import build_query_variants, sanitize_search_title
from .automap_scoring import parse_italian_date

# Season/part suffixes that produce only noise when used as standalone
# mid-window queries (e.g. "Season 2", "3rd Season", "Part 2").
_SEASON_NOISE = re.compile(
    r"^(?:season|part|cour|cours)\s*\d+$|^\d+(?:st|nd|rd|th)\s+(?:season|part|cour)$",
    re.IGNORECASE,
)


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
    # Capture the first episode number listed on the AW page.  For most shows
    # this is 1.  For continuation pages (e.g. a long-runner whose main page
    # was eventually split and a second page opens mid-series) the first number
    # will be greater than 1.  This is used by automap to match each season to
    # the page that actually covers its episode range when scene numbering data
    # is available.
    try:
        aw_first_episode = int(episodes[0]["number"]) if episodes else None
    except (KeyError, TypeError, ValueError):
        aw_first_episode = None
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
        "aw_first_episode": aw_first_episode,
    }


def _mid_window_queries(primary_title: str, alternate_titles: list[dict]) -> list[str]:
    """Sliding 3-word windows across all titles, starting from position 1.

    Only emits windows that are long enough to be distinctive (≥8 chars) and
    are not pure season/part suffix noise (e.g. 'Season 2', '3rd Season').
    Used as try2 last-resort when both primary and unnormalized passes return
    zero results — covers titles where the distinctive words sit mid-string
    and front-anchored truncation never reaches them (e.g. long Japanese
    romanizations like 'Re Zero kara Hajimeru Isekai Seikatsu').
    """
    seen: set[str] = set()
    result: list[str] = []
    raw_values = [primary_title, *[str(item.get("title") or "") for item in alternate_titles]]
    for value in raw_values:
        words = sanitize_search_title(value).split()
        for i in range(1, len(words)):
            window = " ".join(words[i:i + 3]).strip()
            key = window.casefold()
            if len(window) < 8 or key in seen or _SEASON_NOISE.match(window):
                continue
            seen.add(key)
            result.append(window)
    return result


def _raw_titles(primary_title: str, alternate_titles: list[dict]) -> list[str]:
    """Return the raw pre-sanitization title strings, deduped and non-empty."""
    seen: set[str] = set()
    result: list[str] = []
    for value in [primary_title, *[str(item.get("title") or "") for item in alternate_titles]]:
        query = " ".join(str(value or "").split()).strip()
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            result.append(query)
    return result


def _enrich_unique(client: AnimeWorldClient, raw_results: list[dict], limit: int | None) -> list[dict]:
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


def discover_candidates_for_titles(primary_title: str, alternate_titles: list[dict], limit: int | None = None) -> list[dict]:
    client = AnimeWorldClient()
    raw_results: list[dict] = []
    for query in _collect_titles(primary_title, alternate_titles):
        raw_results.extend(client.search(query, limit=None))

    # Try 1: raw pre-sanitization titles.  Sanitization strips chars like / - :
    # that are semantically part of some titles (e.g. "Fate/stay night") and
    # can produce zero results when removed.
    if not raw_results:
        for query in _raw_titles(primary_title, alternate_titles):
            raw_results.extend(client.search(query, limit=None))

    # Try 2: sliding 3-word mid-title windows.  Covers titles where the
    # distinctive searchable words sit past the front of the string and
    # front-anchored truncation never reaches them (e.g. long Japanese
    # romanizations).  Only fires when both primary and try1 returned nothing.
    if not raw_results:
        for query in _mid_window_queries(primary_title, alternate_titles):
            raw_results.extend(client.search(query, limit=None))

    return _enrich_unique(client, raw_results, limit)
