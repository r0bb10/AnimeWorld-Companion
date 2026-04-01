"""Query parsing and local title resolution for the clean rebuild."""

import re

from ..repositories.movies import find_movie_by_title
from ..repositories.shows import find_show_by_title


def _strip_year_tokens(value: str) -> str:
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", "", value or "")
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_:")


def parse_query(query: str) -> dict:
    raw = (query or "").strip()
    if not raw:
        return {"raw": query, "title": "", "season": None, "episode": None}

    match = re.search(r"(.+?)\s+S(\d{1,2})E(\d{1,3})$", raw, re.IGNORECASE)
    if match:
        return {
            "raw": query,
            "title": _strip_year_tokens(match.group(1)),
            "season": int(match.group(2)),
            "episode": int(match.group(3)),
        }

    match = re.search(r"(\d{1,2})x(\d{1,3})", raw, re.IGNORECASE)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        title = re.sub(r"\d{1,2}x\d{1,3}", "", raw, flags=re.IGNORECASE).strip()
        title = _strip_year_tokens(title)
        return {"raw": query, "title": title, "season": season, "episode": episode}

    match = re.search(r"(.+?)\s+S(\d{1,2})\s+(\d{1,3})$", raw, re.IGNORECASE)
    if match:
        return {
            "raw": query,
            "title": _strip_year_tokens(match.group(1)),
            "season": int(match.group(2)),
            "episode": int(match.group(3)),
        }

    match = re.search(r"(.+?)\s+Season\s+(\d{1,2})\s+(\d{1,3})$", raw, re.IGNORECASE)
    if match:
        return {
            "raw": query,
            "title": _strip_year_tokens(match.group(1)),
            "season": int(match.group(2)),
            "episode": int(match.group(3)),
        }

    match = re.search(r"(.+?)\s+(\d{1,2})$", raw)
    if match:
        return {
            "raw": query,
            "title": _strip_year_tokens(match.group(1)),
            "season": None,
            "episode": int(match.group(2)),
        }

    match = re.search(r"(.+?)\s+\((\d{1,3})\)$", raw)
    if match:
        return {
            "raw": query,
            "title": _strip_year_tokens(match.group(1)),
            "season": None,
            "episode": int(match.group(2)),
        }

    return {"raw": query, "title": _strip_year_tokens(raw), "season": None, "episode": None}


def resolve_local_query(query: str, media: str = "show") -> dict:
    parsed = parse_query(query)
    title = parsed["title"]

    if media == "movie":
        match = find_movie_by_title(title)
        return {
            "media": "movie",
            "parsed": parsed,
            "matched": bool(match),
            "result": match,
        }

    match = find_show_by_title(title)
    return {
        "media": "show",
        "parsed": parsed,
        "matched": bool(match),
        "result": match,
    }
