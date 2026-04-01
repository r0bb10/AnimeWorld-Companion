"""Shared title normalization for DB storage and lookup."""

from __future__ import annotations

import re


def normalize_title(value: str) -> str:
    title = (value or "").lower().strip()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()
