"""Process-wide admission control and wire-level auditing for the portal."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import logging
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter

from ..core.config import settings

_operation: ContextVar[tuple[str, str] | None] = ContextVar("animeworld_operation", default=None)
_logical_request: ContextVar[str] = ContextVar("animeworld_logical_request", default="")


def audit(event_type: str, details: dict) -> None:
    logging.getLogger("animeworld.audit").info(
        event_type, extra={"awc_event_type": event_type, "awc_details": details},
    )


@contextmanager
def animeworld_operation(source: str):
    if _operation.get() is not None:
        yield
        return
    token = _operation.set((source, uuid4().hex))
    try:
        yield
    finally:
        _operation.reset(token)


@contextmanager
def animeworld_request():
    token = _logical_request.set(uuid4().hex)
    try:
        yield
    finally:
        _logical_request.reset(token)


def request_context() -> dict:
    source, operation_id = _operation.get() or (threading.current_thread().name, "")
    return {"source": source, "operation_id": operation_id, "logical_request_id": _logical_request.get()}


def safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class AnimeWorldRequestBlocked(requests.RequestException):
    def __init__(self, response: requests.Response | None, seconds: float):
        self.retry_after = str(max(1, int(seconds + 0.999)))
        super().__init__("AnimeWorld requests paused after an access rejection", response=response)


class AnimeWorldTrafficController:
    def __init__(self, interval_ms: int, max_in_flight: int, cooldown_seconds: int):
        self.interval = interval_ms / 1000
        self.limit = max_in_flight
        self.cooldown = cooldown_seconds
        self._condition = threading.Condition()
        self._queue: deque[object] = deque()
        self._last_start: float | None = None
        self._active = 0
        self._blocked_until = 0.0
        self._rejection: requests.Response | None = None
        self._probe = False
        self._sequence = 0
        self._run_id = uuid4().hex
        self._rejection_generation = 0
        self._stopped = False

    def start(self) -> None:
        with self._condition:
            self._stopped = False

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "run_id": self._run_id,
                "interval_ms": round(self.interval * 1000), "max_in_flight": self.limit,
                "cooldown_seconds": self.cooldown,
                "active": self._active, "waiting": len(self._queue), "requests": self._sequence,
                "cooldown_remaining_seconds": round(max(0, self._blocked_until - time.monotonic()), 3),
                "recovery_probe": self._probe,
                "rejection_generation": self._rejection_generation,
            }

    def acquire(self, method: str, url: str) -> dict:
        ticket = object()
        queued = time.monotonic()
        details: dict = {"request_id": uuid4().hex, "run_id": self._run_id, **request_context(),
                         "method": method, "endpoint": safe_endpoint(url)}
        with self._condition:
            self._queue.append(ticket)
            try:
                while True:
                    if self._stopped:
                        raise requests.RequestException("AnimeWorld transport is shutting down")
                    now = time.monotonic()
                    if now < self._blocked_until:
                        details.update(sent=False, queue_wait_ms=round((now - queued) * 1000, 3))
                        audit("animeworld.http.blocked", details)
                        raise AnimeWorldRequestBlocked(self._rejection, self._blocked_until - now)
                    delay = 0 if self._last_start is None else max(0, self._last_start + self.interval - now)
                    if self._queue[0] is ticket and self._active < self.limit and not self._probe and delay <= 0:
                        self._last_start = now
                        self._active += 1
                        self._sequence += 1
                        self._probe = self._rejection is not None
                        details.update(
                            sent=True, sequence=self._sequence, started_at=datetime.now(UTC).isoformat(),
                            queue_wait_ms=round((now - queued) * 1000, 3), active_at_start=self._active,
                            interval_ms=round(self.interval * 1000), max_in_flight=self.limit,
                            recovery_probe=self._probe,
                            rejection_generation=self._rejection_generation,
                        )
                        return details
                    self._condition.wait(timeout=delay if delay > 0 else None)
            finally:
                self._queue.remove(ticket)
                self._condition.notify_all()

    def reject(self, response: requests.Response) -> None:
        if response.status_code not in {403, 429}:
            return
        with self._condition:
            seconds = max(self.cooldown, retry_after_seconds(response.headers.get("Retry-After")))
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
            self._rejection = response
            self._rejection_generation += 1
            self._condition.notify_all()
        details = {"status_code": response.status_code, "cooldown_seconds": seconds,
                   "endpoint": safe_endpoint(str(response.url or ""))}
        audit("animeworld.http.paused", details)

    def release(self, details: dict) -> None:
        with self._condition:
            self._active -= 1
            if details["recovery_probe"]:
                self._probe = False
                if (details.get("status_code", 0) in range(200, 400) and "error_kind" not in details
                        and details["rejection_generation"] == self._rejection_generation):
                    self._rejection = None
                    audit("animeworld.http.resumed", {"run_id": self._run_id})
                else:
                    self._blocked_until = max(self._blocked_until, time.monotonic() + self.cooldown)
            self._condition.notify_all()
        audit("animeworld.http.request", details)


traffic = AnimeWorldTrafficController(
    settings.aw_request_interval_ms, settings.aw_max_in_flight, settings.aw_access_cooldown_seconds,
)


class AnimeWorldHTTPAdapter(HTTPAdapter):
    def __init__(self, controller: AnimeWorldTrafficController | None = None):
        controller = controller if controller is not None else traffic
        super().__init__(pool_connections=controller.limit, pool_maxsize=controller.limit, max_retries=0)
        self.controller = controller

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if request.method is None or request.url is None:
            raise requests.RequestException("AnimeWorld request must have a method and URL")
        details = self.controller.acquire(request.method, request.url)
        started = time.monotonic()
        response = None
        try:
            response = super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)
            details.update(status_code=response.status_code, retry_after=response.headers.get("Retry-After"))
            self.controller.reject(response)
            if response.is_redirect:
                destination = urlsplit(urljoin(request.url, response.headers["Location"]))
                if destination.netloc != urlsplit(request.url).netloc:
                    raise requests.RequestException("Cross-origin AnimeWorld redirect refused", response=response)
            details["response_bytes"] = len(response.content)
            return response
        except Exception as exc:
            details["error_kind"] = type(exc).__name__
            if response is not None:
                response.close()
            raise
        finally:
            details["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.controller.release(details)


def mount_animeworld_transport(session: requests.Session, base_url: str) -> None:
    parts = urlsplit(base_url)
    # Exact origin prefix: neither other services nor CDN hosts use this adapter.
    for scheme in ("http", "https"):
        session.mount(f"{scheme}://{parts.netloc}/", AnimeWorldHTTPAdapter())
