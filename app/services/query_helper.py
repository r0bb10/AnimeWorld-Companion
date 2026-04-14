"""Shared AnimeWorld query shaping helpers."""

from __future__ import annotations

import re


def sanitize_search_title(title: str) -> str:
    """Remove punctuation that tends to reduce AnimeWorld search recall."""
    value = str(title or "")
    chars_to_space = "/:\\!?\"()[]{}~@#$%^&*+=|<>,-"
    for char in chars_to_space:
        value = value.replace(char, " ")
    return " ".join(value.split()).strip()


def extract_base_name(title: str) -> str:
    """Trim common season/special suffixes to broaden AnimeWorld discovery."""
    value = str(title or "").strip()
    if not value:
        return value

    patterns = [
        r"\s+(?:season|part|cour|cours|serie|series)\s+\d+.*$",
        r"\s+\d+(?:st|nd|rd|th)\s+(?:season|part|cour).*$",
        r"\s+s\d+.*$",
        r"\s+(?:final|last|complete|movie|ova|ona|special).*$",
        r"\s+(?:zoku|kan|gaiden|gekijouban|movie)\b.*$",
    ]

    result = value
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    return " ".join(result.split()).strip()


def build_query_variants(title: str) -> list[str]:
    """Generate bounded, deduped discovery variants for one title."""
    seen: set[str] = set()
    variants: list[str] = []

    def add(value: str) -> None:
        query = " ".join(str(value or "").split()).strip()
        if not query:
            return
        key = query.casefold()
        if key in seen:
            return
        seen.add(key)
        variants.append(query)

    raw = " ".join(str(title or "").split()).strip()
    add(raw)

    sanitized = sanitize_search_title(title)
    add(sanitized)

    base_name = extract_base_name(sanitized)
    if base_name and base_name.casefold() != sanitized.casefold():
        add(base_name)

    words = sanitized.split()
    if len(words) >= 3:
        add(" ".join(words[:3]))
    if len(words) >= 2:
        add(" ".join(words[:2]))
    if words and len(words[0]) >= 5:
        add(words[0])

    return variants
