"""AnimeWorld request failures shared by clients and services."""

from http import HTTPStatus
from concurrent.futures import Future
import json
import threading

import requests

from .animeworld_transport import (
    AnimeWorldHTTPAdapter, AnimeWorldRequestBlocked, animeworld_operation, animeworld_request, audit,
    mount_animeworld_transport, request_context, safe_endpoint,
)

_FLIGHTS_LOCK = threading.Lock()
_FLIGHTS: dict[tuple, tuple[Future, str]] = {}


class AnimeWorldAccessError(requests.RequestException):
    def __init__(self, error: requests.RequestException, endpoint: str):
        response = error.response
        self.status_code = response.status_code if response is not None else None
        self.endpoint = safe_endpoint(endpoint)
        self.retry_after = response.headers.get("Retry-After") if response is not None else None
        if isinstance(error, AnimeWorldRequestBlocked):
            self.kind = "cooldown"
            self.retry_after = error.retry_after
        elif self.status_code in {401, 403}:
            self.kind = "access_denied"
        elif self.status_code == 429:
            self.kind = "rate_limited"
        elif self.status_code in {404, 410}:
            self.kind = "not_found"
        elif self.status_code is not None and self.status_code >= 500:
            self.kind = "upstream_error"
        elif self.status_code is not None:
            self.kind = "http_error"
        elif isinstance(error, requests.Timeout):
            self.kind = "timeout"
        elif isinstance(error, requests.ConnectionError):
            self.kind = "connection_error"
        else:
            self.kind = "request_error"
        try:
            self.reason = HTTPStatus(self.status_code).phrase if self.status_code is not None else self.kind.replace("_", " ")
        except ValueError:
            self.reason = "Upstream HTTP error"
        status = f"{self.status_code} {self.reason}" if self.status_code is not None else self.reason
        super().__init__(f"AnimeWorld: {status} ({self.endpoint})", response=response)

    @property
    def blocks_requests(self) -> bool:
        return self.kind == "cooldown" or self.status_code in {403, 429}

    def details(self) -> dict:
        return {
            "kind": self.kind,
            "status_code": self.status_code,
            "reason": self.reason,
            "endpoint": self.endpoint,
            "retry_after": self.retry_after,
        }


def request_animeworld(method: str, url: str, *, session: requests.Session | None = None, **kwargs) -> requests.Response:
    with animeworld_operation(threading.current_thread().name), animeworld_request():
        if method.upper() not in {"GET", "HEAD"} or kwargs.get("stream") or "data" in kwargs or "json" in kwargs:
            return _request_animeworld(method, url, session=session, **kwargs)
        key = (method.upper(), url, id(session), json.dumps(kwargs, sort_keys=True, default=str))
        future: Future[requests.Response] = Future()
        with _FLIGHTS_LOCK:
            existing = _FLIGHTS.get(key)
            if existing is None:
                _FLIGHTS[key] = (future, request_context()["logical_request_id"])
        if existing is not None:
            future, owner_id = existing
            audit("animeworld.http.coalesced", {
                **request_context(), "owner_logical_request_id": owner_id,
                "method": method.upper(), "endpoint": safe_endpoint(url), "sent": False,
            })
            return future.result()
        try:
            response = _request_animeworld(method, url, session=session, **kwargs)
            future.set_result(response)
            return response
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with _FLIGHTS_LOCK:
                del _FLIGHTS[key]


def _request_animeworld(method: str, url: str, *, session: requests.Session | None = None, **kwargs) -> requests.Response:
    try:
        if session is None:
            with requests.Session() as standalone:
                mount_animeworld_transport(standalone, url)
                response = standalone.request(method, url, **kwargs)
        else:
            if isinstance(session, requests.Session):
                prepared = requests.PreparedRequest()
                prepared.prepare_url(url, None)
                if not isinstance(session.get_adapter(str(prepared.url)), AnimeWorldHTTPAdapter):
                    audit("animeworld.http.refused", {
                        **request_context(), "endpoint": safe_endpoint(url), "sent": False,
                        "reason": "outside_controlled_portal_origin",
                    })
                    raise requests.RequestException("AnimeWorld URL is outside the controlled portal origin")
            response = session.request(method, url, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise AnimeWorldAccessError(exc, url) from exc
