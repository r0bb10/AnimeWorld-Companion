"""Shared release-window helpers for aired/released checks."""

from __future__ import annotations

from datetime import UTC, date, datetime


def utc_today() -> date:
    return datetime.now(UTC).date()


def utc_today_iso() -> str:
    return utc_today().isoformat()


def parse_iso_day(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def has_started(value: object) -> bool:
    parsed = parse_iso_day(value)
    return bool(parsed and parsed <= utc_today())
