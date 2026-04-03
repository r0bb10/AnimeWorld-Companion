"""Log query helpers."""

from __future__ import annotations

from ..repositories.log_history import query_log_events


def build_log_snapshot(
    *,
    level: str = "",
    logger_name: str = "",
    event_type: str = "",
    since: str = "",
    until: str = "",
    q: str = "",
    limit: int = 200,
) -> dict:
    items = query_log_events(
        level=level,
        logger_name=logger_name,
        event_type=event_type,
        since=since,
        until=until,
        q=q,
        limit=limit,
    )
    return {
        "count": len(items),
        "items": items,
    }
