"""Lightweight SSE stream for runtime events."""

import json
import time

from .dashboard_service import build_heartbeat_snapshot
from .download_service import build_download_snapshot


def build_event_payload() -> dict:
    downloads = build_download_snapshot(limit=20)
    return {
        "heartbeat": build_heartbeat_snapshot(),
        "downloads": downloads["downloads"],
    }


def stream_events():
    while True:
        payload = build_event_payload()
        yield f"event: heartbeat\ndata: {json.dumps(payload['heartbeat'])}\n\n"
        yield f"event: downloads\ndata: {json.dumps({'downloads': payload['downloads']})}\n\n"
        time.sleep(10)
