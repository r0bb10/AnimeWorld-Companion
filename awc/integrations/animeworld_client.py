"""AnimeWorld client for search, episode discovery, and file resolution."""

from dataclasses import dataclass
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

from ..core.config import settings

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_PAGE_CACHE_TTL = 60
_PAGE_CACHE: dict[str, tuple[BeautifulSoup, str, float]] = {}
_PAGE_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_TTL = 300
_SEARCH_CACHE: dict[tuple[str, int | None], tuple[list[dict], float]] = {}
_SEARCH_CACHE_LOCK = threading.Lock()
_META_CACHE_TTL = 300
_META_CACHE: dict[str, tuple[dict, list[dict], str, bool, float]] = {}
_META_CACHE_LOCK = threading.Lock()
_VERIFY_CACHE_TTL = 300
_VERIFY_CACHE: dict[str, tuple[str, int, float]] = {}
_VERIFY_CACHE_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_SESSION: requests.Session | None = None
_POOL_MAXSIZE = 64


@dataclass(frozen=True)
class AnimeWorldHealth:
    ok: bool
    url: str


def _session_base_url() -> str:
    return settings.aw_base_url.rstrip("/")


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    adapter = HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _bootstrap_session(session: requests.Session, base_url: str) -> requests.Session:
    response = session.get(base_url, timeout=15)
    response.raise_for_status()
    match = re.search(r'<meta[^>]+id="csrf-token"[^>]+content="([^"]+)"', response.text)
    if match:
        session.headers["csrf-token"] = match.group(1)
    return session


def _get_shared_session(base_url: str) -> requests.Session:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = _bootstrap_session(_new_session(), base_url)
        return _SESSION


def _reset_shared_session(base_url: str) -> requests.Session:
    global _SESSION
    with _SESSION_LOCK:
        _SESSION = _bootstrap_session(_new_session(), base_url)
        return _SESSION


class AnimeWorldClient:
    """AnimeWorld client with shared bootstrap, page caching, and API-first search."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.aw_base_url).rstrip("/")

    def _session(self) -> requests.Session:
        return _get_shared_session(self.base_url)

    def _post(self, path: str, **kwargs) -> requests.Response:
        session = self._session()
        try:
            response = session.post(f"{self.base_url}{path}", timeout=15, **kwargs)
            if response.status_code in {401, 403}:
                raise requests.HTTPError(response=response)
            return response
        except (requests.HTTPError, requests.Timeout):
            session = _reset_shared_session(self.base_url)
            return session.post(f"{self.base_url}{path}", timeout=15, **kwargs)

    def _get_page_details(self, url: str) -> tuple[BeautifulSoup | None, str]:
        now = time.time()
        with _PAGE_CACHE_LOCK:
            cached = _PAGE_CACHE.get(url)
            if cached and now - cached[2] < _PAGE_CACHE_TTL:
                return cached[0], cached[1]

        try:
            response = self._session().get(url, timeout=15)
            response.raise_for_status()
        except requests.RequestException:
            return None, url

        soup = BeautifulSoup(response.text, "html.parser")
        final_url = str(response.url or url)
        with _PAGE_CACHE_LOCK:
            _PAGE_CACHE[url] = (soup, final_url, time.time())
        return soup, final_url

    def _get_page(self, url: str) -> BeautifulSoup | None:
        soup, _ = self._get_page_details(url)
        return soup

    def _extract_episodes(self, soup: BeautifulSoup) -> list[dict]:
        episodes = []
        for anchor in soup.select("li.episode a[data-episode-num]"):
            number = anchor.get("data-episode-num") or anchor.get("data-num") or ""
            raw_number = anchor.get("data-num") or anchor.get("data-base") or number
            episodes.append(
                {
                    "number": number,
                    "number_raw": raw_number,
                    "episode_id": anchor.get("data-episode-id") or anchor.get("data-id") or "",
                    "href": anchor.get("href", ""),
                }
            )
        return episodes

    def _extract_info(self, soup: BeautifulSoup) -> dict:
        info: dict[str, object] = {}
        info_div = soup.find("div", class_="info")
        if info_div:
            for dt, dd in zip(info_div.find_all("dt"), info_div.find_all("dd")):
                key = dt.get_text(" ", strip=True).rstrip(":")
                links = dd.find_all("a")
                if links:
                    values = [anchor.get_text(" ", strip=True) for anchor in links]
                    info[key] = values[0] if len(values) == 1 else values
                else:
                    info[key] = dd.get_text(" ", strip=True)
        return info

    def health(self) -> AnimeWorldHealth:
        if not self.base_url:
            return AnimeWorldHealth(ok=False, url="")
        try:
            response = self._session().get(self.base_url, timeout=8)
            response.raise_for_status()
            return AnimeWorldHealth(ok=True, url=self.base_url)
        except requests.RequestException:
            return AnimeWorldHealth(ok=False, url=self.base_url)

    def slug_to_url(self, slug: str) -> str:
        return f"{self.base_url}/play/{slug.strip('/')}/"

    def url_to_slug(self, url: str) -> str:
        if not url:
            return ""
        if "/play/" in url:
            return url.split("/play/", 1)[1].split("/")[0]
        return url.strip("/")

    def _search_v2(self, query: str, limit: int | None = None) -> list[dict]:
        response = self._post("/api/search/v2", params={"keyword": query.strip()})
        response.raise_for_status()
        payload = response.json()
        results = []
        items = payload.get("animes", [])
        if limit is not None:
            items = items[:limit]
        for item in items:
            slug = item.get("link") or ""
            identifier = item.get("identifier") or ""
            full_slug = f"{slug}.{identifier}" if slug and identifier and "." not in slug else slug
            url = self.slug_to_url(full_slug) if full_slug and not full_slug.startswith("http") else slug
            dub_value = item.get("dub", 0)
            results.append(
                {
                    "title": item.get("name") or item.get("title") or "",
                    "japanese_title": item.get("jtitle") or item.get("japanese_title") or "",
                    "url": url,
                    "slug": self.url_to_slug(url or full_slug),
                    "poster": item.get("image") or item.get("poster") or "",
                    "kind": item.get("type") or item.get("status") or "",
                    "dub": int(dub_value) != 0 if str(dub_value).isdigit() else bool(dub_value),
                    "episodes": int(item.get("episodes") or 0) if str(item.get("episodes", "")).isdigit() else 0,
                }
            )
        return results

    def _search_scrape(self, query: str, limit: int | None = None) -> list[dict]:
        response = self._session().get(
            f"{self.base_url}/search",
            params={"keyword": query.strip()},
            timeout=15,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        items = soup.select(".film-list .item")
        if limit is not None:
            items = items[:limit]
        for item in items:
            name_link = item.select_one("a.name")
            poster_link = item.select_one("a.poster")
            image = item.select_one("img")
            status = item.select_one(".status div")
            href = (name_link or poster_link).get("href", "") if (name_link or poster_link) else ""
            full_url = href if href.startswith("http") else f"{self.base_url}{href}"
            results.append(
                {
                    "title": (name_link.get_text(" ", strip=True) if name_link else item.get_text(" ", strip=True)),
                    "japanese_title": name_link.get("data-jtitle", "") if name_link else "",
                    "url": full_url,
                    "slug": self.url_to_slug(full_url),
                    "poster": image.get("src", "") if image else "",
                    "kind": status.get_text(" ", strip=True) if status else "",
                }
            )
        return results

    def search(self, query: str, limit: int | None = None) -> list[dict]:
        normalized_query = query.strip()
        if not self.base_url or not normalized_query:
            return []
        cache_key = (normalized_query.casefold(), limit)
        now = time.monotonic()
        with _SEARCH_CACHE_LOCK:
            cached = _SEARCH_CACHE.get(cache_key)
            if cached and now - cached[1] < _SEARCH_CACHE_TTL:
                return [dict(item) for item in cached[0]]

        merged: list[dict] = []
        seen: set[str] = set()

        for search_fn in (self._search_v2, self._search_scrape):
            try:
                results = search_fn(normalized_query, limit)
            except Exception:
                results = []
            for item in results:
                key = self.url_to_slug(item.get("url") or item.get("slug", ""))
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        if limit is not None:
            merged = merged[:limit]
        with _SEARCH_CACHE_LOCK:
            _SEARCH_CACHE[cache_key] = ([dict(item) for item in merged], now)
        return [dict(item) for item in merged]

    def get_episodes(self, slug_or_url: str) -> list[dict]:
        _, episodes, _, _ = self.get_info_and_episodes_meta(slug_or_url)
        return [dict(item) for item in episodes]

    def get_info_and_episodes(self, slug_or_url: str) -> tuple[dict, list[dict]]:
        info, episodes, _, _ = self.get_info_and_episodes_meta(slug_or_url)
        return info, episodes

    def get_info_and_episodes_meta(self, slug_or_url: str) -> tuple[dict, list[dict], str, bool]:
        target = slug_or_url if slug_or_url.startswith("http") else self.slug_to_url(slug_or_url)
        now = time.monotonic()
        with _META_CACHE_LOCK:
            cached = _META_CACHE.get(target)
            if cached and now - cached[4] < _META_CACHE_TTL:
                info, episodes, final_url, is_placeholder, _ = cached
                return dict(info), [dict(item) for item in episodes], final_url, is_placeholder

        soup, final_url = self._get_page_details(target)
        if soup is None:
            return {}, [], target, False

        info = self._extract_info(soup)
        episodes = self._extract_episodes(soup)
        final_url_normalized = final_url.rstrip("/")
        is_placeholder = final_url_normalized.endswith("/tba")
        if not is_placeholder:
            body_text = soup.get_text(" ", strip=True).lower()
            is_placeholder = bool(not episodes and "tba" in final_url_normalized.lower()) or ("coming soon" in body_text and not episodes)
        with _META_CACHE_LOCK:
            _META_CACHE[target] = (dict(info), [dict(item) for item in episodes], final_url, is_placeholder, now)
        return dict(info), [dict(item) for item in episodes], final_url, is_placeholder

    def verify_slug(self, slug_or_url: str) -> tuple[str, int]:
        target = slug_or_url if slug_or_url.startswith("http") else self.slug_to_url(slug_or_url)
        now = time.monotonic()
        with _VERIFY_CACHE_LOCK:
            cached = _VERIFY_CACHE.get(target)
            if cached and now - cached[2] < _VERIFY_CACHE_TTL:
                return cached[0], cached[1]
        response = self._session().get(target, timeout=15, allow_redirects=True)
        response.raise_for_status()
        final_slug = self.url_to_slug(str(response.url or target)) or self.url_to_slug(target)
        with _VERIFY_CACHE_LOCK:
            _VERIFY_CACHE[target] = (final_slug, int(response.status_code), now)
        return final_slug, int(response.status_code)

    def get_file_info(self, episode_id: str) -> list[dict]:
        if not episode_id:
            return []
        try:
            response = self._post(f"/api/download/{episode_id}")
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        links = data.get("links", {}) if isinstance(data, dict) else {}
        server9 = links.get("9", {}) if isinstance(links, dict) else {}
        episode_data = {}
        for value in server9.values():
            if isinstance(value, dict) and ("alternativeLink" in value or "link" in value):
                episode_data = value
                break

        cdn_url = episode_data.get("alternativeLink") or episode_data.get("link") or ""
        if "download-file.php?id=" in cdn_url:
            cdn_url = cdn_url.replace("download-file.php?id=", "")
        if not cdn_url:
            return []

        total_bytes = 0
        last_modified = None
        try:
            head = self._session().head(cdn_url, timeout=8, allow_redirects=True)
            total_bytes = int(head.headers.get("Content-Length", 0))
            last_modified = head.headers.get("Last-Modified")
        except Exception:
            pass

        return [
            {
                "url": cdn_url,
                "total_bytes": total_bytes,
                "last_modified": last_modified,
                "server_name": "AnimeWorld Server",
            }
        ]

    def parse_episode_count_value(self, value: object) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def count_non_special_episodes(self, episodes: list[dict]) -> tuple[int, int, int]:
        total = 0
        non_special = 0
        for item in episodes:
            number = str(item.get("number_raw") or item.get("number") or "").strip()
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", number)
            if match:
                start = int(match.group(1))
                end = int(match.group(2))
                if end >= start:
                    span = end - start + 1
                    total += span
                    non_special += max(0, end - max(start, 1) + 1)
                    continue
            try:
                parsed = float(number)
                if parsed.is_integer() and int(parsed) >= 1:
                    total += 1
                    non_special += 1
                    continue
            except Exception:
                pass
            total += 1
        return non_special, total, total - non_special
