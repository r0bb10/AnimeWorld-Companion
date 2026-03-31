"""Lightweight SSE stream for runtime events."""

import json
import time

from .dashboard_service import build_dashboard_snapshot
from .download_service import build_download_snapshot


def build_event_payload() -> dict:
    dashboard = build_dashboard_snapshot()
    downloads = build_download_snapshot(limit=20)
    return {
        "heartbeat": dashboard,
        "downloads": downloads,
    }


def stream_events():
    while True:
        payload = build_event_payload()
        yield f"event: heartbeat\ndata: {json.dumps(payload)}\n\n"
        time.sleep(10)
