"""Lightweight SSE stream for runtime and library events."""

from __future__ import annotations

import json
from queue import Empty, SimpleQueue
import threading
import time

_sse_shutdown = threading.Event()
_subscriber_lock = threading.Lock()
_subscribers: list[SimpleQueue] = []
_PERIODIC_INTERVAL = 10.0


def build_event_payload() -> dict:
    from .dashboard_service import build_heartbeat_snapshot
    from .download_service import build_download_snapshot

    downloads = build_download_snapshot(limit=20)
    return {
        "heartbeat": build_heartbeat_snapshot(),
        "downloads": downloads["downloads"],
    }


def _subscribe() -> SimpleQueue:
    queue: SimpleQueue = SimpleQueue()
    with _subscriber_lock:
        _subscribers.append(queue)
    return queue


def _unsubscribe(queue: SimpleQueue) -> None:
    with _subscriber_lock:
        if queue in _subscribers:
            _subscribers.remove(queue)


def publish_event(event_type: str, payload: dict) -> None:
    with _subscriber_lock:
        subscribers = list(_subscribers)
    for queue in subscribers:
        queue.put((event_type, payload))


def publish_library_card_changed(kind: str, item_id: int) -> None:
    publish_event("library_card_changed", {"kind": str(kind), "id": int(item_id)})


def publish_library_card_removed(kind: str, item_id: int) -> None:
    publish_event("library_card_removed", {"kind": str(kind), "id": int(item_id)})


def publish_library_stats_changed() -> None:
    publish_event("library_stats_changed", {})


def publish_library_batch(name: str, status: str, **details) -> None:
    payload = {"name": str(name), "status": str(status)}
    if details:
        payload["details"] = details
    publish_event("library_batch", payload)


def start_sse_streams() -> None:
    _sse_shutdown.clear()


def stop_sse_streams() -> None:
    _sse_shutdown.set()


def stream_events():
    queue = _subscribe()
    next_periodic = 0.0
    try:
        while not _sse_shutdown.is_set():
            now = time.time()
            if now >= next_periodic:
                payload = build_event_payload()
                yield f"event: heartbeat\ndata: {json.dumps(payload['heartbeat'])}\n\n"
                yield f"event: downloads\ndata: {json.dumps({'downloads': payload['downloads']})}\n\n"
                next_periodic = now + _PERIODIC_INTERVAL
            try:
                event_type, payload = queue.get(timeout=1.0)
            except Empty:
                continue
            yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
    finally:
        _unsubscribe(queue)
