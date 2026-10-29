"""AnimeWorld request failures shared by clients and services."""

from http import HTTPStatus

import requests

from .animeworld_transport import AnimeWorldRequestBlocked, mount_animeworld_transport, safe_endpoint


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

    def details(self) -> dict:
        return {
            "kind": self.kind,
            "status_code": self.status_code,
            "reason": self.reason,
            "endpoint": self.endpoint,
            "retry_after": self.retry_after,
        }


def request_animeworld(method: str, url: str, *, session: requests.Session | None = None, **kwargs) -> requests.Response:
    try:
        if session is None:
            with requests.Session() as standalone:
                mount_animeworld_transport(standalone, url)
                response = standalone.request(method, url, **kwargs)
        else:
            response = session.request(method, url, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise AnimeWorldAccessError(exc, url) from exc
