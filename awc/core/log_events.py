"""Human-readable domain log helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import unquote, urlsplit

from .config import settings


def log_block(logger: logging.Logger, level: int, header: str, lines: list[str] | None = None) -> None:
    logger.log(level, header)
    for line in lines or []:
        logger.log(level, "  ↳ %s", line)


def _season_label(number: int) -> str:
    return f"S{int(number):02d}"


def _score_pct(value: float | int | None) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "--"


def _load_factors(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def display_aw_link(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"{settings.aw_base_url.rstrip('/')}/play/{raw.strip('/')}/"


def format_show_automap_lines(show: dict, mapped_seasons: list[int], ambiguous: list[dict] | None = None) -> list[str]:
    seasons = {
        int(season.get("season_number") or 0): season
        for season in show.get("seasons", [])
    }
    lines: list[str] = []
    linked_groups: dict[int, list[int]] = {}

    for sn in sorted(mapped_seasons):
        season = seasons.get(int(sn))
        if not season:
            continue
        mappings = sorted(season.get("mappings") or [], key=lambda item: int(item.get("part") or 0))
        if not mappings:
            continue
        if all(mapping.get("linked_with_season") for mapping in mappings):
            root = int(mappings[0].get("linked_with_season") or 0)
            linked_groups.setdefault(root, []).append(sn)
            continue
        if len(mappings) > 1:
            lines.append(f"{_season_label(sn)} split")
            for mapping in mappings:
                lines.append(
                    f"P{int(mapping.get('part') or 0)} → {display_aw_link(mapping.get('aw_link'))} ({_score_pct(mapping.get('confidence_score'))}) ✓"
                )
            continue
        mapping = mappings[0]
        lines.append(
            f"{_season_label(sn)} → {display_aw_link(mapping.get('aw_link'))} ({_score_pct(mapping.get('confidence_score'))}) ✓"
        )

    for root, linked in sorted(linked_groups.items()):
        joined = "+".join(_season_label(item) for item in sorted(linked))
        lines.append(f"{joined} linked with {_season_label(root)}")

    if ambiguous:
        for item in ambiguous:
            lines.append(f"{_season_label(int(item.get('season') or 0))} needs review")
    return lines


def format_movie_automap_lines(mapping: dict | None) -> list[str]:
    if not mapping:
        return []
    return [f"{display_aw_link(mapping.get('aw_link'))} ({_score_pct(mapping.get('confidence_score'))}) ✓"]


def extract_remote_filename(headers: dict, fallback_url: str = "") -> str:
    disposition = headers.get("Content-Disposition", "") or headers.get("content-disposition", "")
    if disposition:
        match = re.search(r"filename\\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1)).strip()
        match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    path = urlsplit(fallback_url).path
    name = os.path.basename(path or "")
    return unquote(name) if name else ""
