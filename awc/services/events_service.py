"""Lightweight SSE stream for runtime events."""

import json
import threading
import time

from .dashboard_service import build_heartbeat_snapshot
from .download_service import build_download_snapshot

_sse_shutdown = threading.Event()


def build_event_payload() -> dict:
    downloads = build_download_snapshot(limit=20)
    return {
        "heartbeat": build_heartbeat_snapshot(),
        "downloads": downloads["downloads"],
    }


def start_sse_streams() -> None:
    _sse_shutdown.clear()


def stop_sse_streams() -> None:
    _sse_shutdown.set()


def stream_events():
    while not _sse_shutdown.is_set():
        payload = build_event_payload()
        yield f"event: heartbeat\ndata: {json.dumps(payload['heartbeat'])}\n\n"
        yield f"event: downloads\ndata: {json.dumps({'downloads': payload['downloads']})}\n\n"
        if _sse_shutdown.wait(10):
            break
