"""AnimeWorld client for search, episode discovery, and file resolution."""

from dataclasses import dataclass
import re
import threading
import time
from typing import TypedDict

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from bs4.element import Tag

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
_VERIFY_CACHE: dict[str, tuple["AnimeWorldVerification", float]] = {}
_VERIFY_CACHE_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_SESSION: requests.Session | None = None
_POOL_MAXSIZE = 64
_SOFT_404_TITLE_MARKERS = ("pagina non trovata", "page not found")
_SOFT_404_META_MARKERS = ("questa pagina non esiste", "this page does not exist")


@dataclass(frozen=True)
class AnimeWorldHealth:
    ok: bool
    url: str


class AnimeWorldVerification(TypedDict):
    final_slug: str
    status_code: int
    is_soft_404: bool
    is_placeholder: bool
    has_episode_ids: bool
    has_info_block: bool
    title: str
    meta_description: str
    redirected_to_episode: bool


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
                    "episode_id": anchor.get("data-episode-id") or "",
                    "data_id": anchor.get("data-id") or "",
                    "href": anchor.get("href", ""),
                }
            )
        return episodes

    def _extract_info(self, soup: BeautifulSoup) -> dict:
        info: dict[str, object] = {}
        info_div = soup.find("div", class_="info")
        if not isinstance(info_div, Tag):
            return info
        for dt, dd in zip(info_div.find_all("dt"), info_div.find_all("dd")):
            if not isinstance(dt, Tag) or not isinstance(dd, Tag):
                continue
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
            kind_value = str(item.get("type") or "")
            # V2 API signals dubbed via either a non-zero `dub` integer (older
            # entries) or a `type: "DUB"` string (newer entries).  Accept both.
            is_dub = (int(dub_value) != 0 if str(dub_value).isdigit() else bool(dub_value)) or kind_value.upper() == "DUB"
            results.append(
                {
                    "title": item.get("name") or item.get("title") or "",
                    "japanese_title": item.get("jtitle") or item.get("japanese_title") or "",
                    "url": url,
                    "slug": self.url_to_slug(url or full_slug),
                    "poster": item.get("image") or item.get("poster") or "",
                    "kind": kind_value,
                    "dub": is_dub,
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
            link = name_link if isinstance(name_link, Tag) else poster_link if isinstance(poster_link, Tag) else None
            href_value = link.get("href", "") if link else ""
            href = href_value if isinstance(href_value, str) else ""
            full_url = href if href.startswith("http") else f"{self.base_url}{href}"
            results.append(
                {
                    "title": (name_link.get_text(" ", strip=True) if isinstance(name_link, Tag) else item.get_text(" ", strip=True)),
                    "japanese_title": str(name_link.get("data-jtitle", "")) if isinstance(name_link, Tag) else "",
                    "url": full_url,
                    "slug": self.url_to_slug(full_url),
                    "poster": str(image.get("src", "")) if isinstance(image, Tag) else "",
                    "kind": status.get_text(" ", strip=True) if isinstance(status, Tag) else "",
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

    def _verify_page_details(self, html: str, final_url: str, final_slug: str, status_code: int) -> AnimeWorldVerification:
        text = html or ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        title = " ".join((title_match.group(1) if title_match else "").split()).strip()
        meta_match = re.search(
            r"""<meta[^>]+name=["']description["'][^>]+content=["'](.*?)["']""",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        meta_description = " ".join((meta_match.group(1) if meta_match else "").split()).strip()

        has_episode_ids = bool(re.search(r"""data-episode-id=["']?\d+""", text, flags=re.IGNORECASE))
        has_info_block = bool(
            re.search(r"""class=["'][^"']*\binfo\b[^"']*["']""", text, flags=re.IGNORECASE)
        )

        title_lower = title.casefold()
        meta_lower = meta_description.casefold()
        has_not_found_marker = any(marker in title_lower for marker in _SOFT_404_TITLE_MARKERS) or any(
            marker in meta_lower for marker in _SOFT_404_META_MARKERS
        )
        is_soft_404 = bool(status_code == 200 and has_not_found_marker and (not has_episode_ids or not has_info_block))

        final_url_normalized = final_url.rstrip("/")
        final_url_lower = final_url_normalized.casefold()
        body_lower = text.casefold()
        is_placeholder = final_url_lower.endswith("/tba") or (
            not has_episode_ids and ("tba" in final_url_lower or "coming soon" in body_lower)
        )

        redirected_to_episode = bool(
            re.search(rf"""/play/{re.escape(final_slug)}/[^/?#]+/?$""", final_url_normalized)
            and not final_url_lower.endswith("/tba")
        )

        return {
            "final_slug": final_slug,
            "status_code": int(status_code),
            "is_soft_404": is_soft_404,
            "is_placeholder": is_placeholder,
            "has_episode_ids": has_episode_ids,
            "has_info_block": has_info_block,
            "title": title,
            "meta_description": meta_description,
            "redirected_to_episode": redirected_to_episode,
        }

    def verify_slug_details(self, slug_or_url: str) -> AnimeWorldVerification:
        target = slug_or_url if slug_or_url.startswith("http") else self.slug_to_url(slug_or_url)
        now = time.monotonic()
        with _VERIFY_CACHE_LOCK:
            cached = _VERIFY_CACHE.get(target)
            if cached and now - cached[1] < _VERIFY_CACHE_TTL:
                return cached[0].copy()

        response = self._session().get(target, timeout=15, allow_redirects=True)
        response.raise_for_status()
        final_url = str(response.url or target)
        final_slug = self.url_to_slug(final_url) or self.url_to_slug(target)
        details = self._verify_page_details(response.text, final_url, final_slug, int(response.status_code))
        with _VERIFY_CACHE_LOCK:
            _VERIFY_CACHE[target] = (details.copy(), now)
        return details.copy()

    def verify_slug(self, slug_or_url: str) -> tuple[str, int]:
        details = self.verify_slug_details(slug_or_url)
        return details["final_slug"], details["status_code"]

    def get_file_info(self, episode_id: str) -> list[dict]:
        if not episode_id:
            return []
        try:
            response = self._session().get(
                f"{self.base_url}/api/episode/info",
                params={"id": episode_id, "alt": 0},
                timeout=15,
            )
            if response.status_code in {401, 403}:
                session = _reset_shared_session(self.base_url)
                response = session.get(
                    f"{self.base_url}/api/episode/info",
                    params={"id": episode_id, "alt": 0},
                    timeout=15,
                )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        cdn_url = data.get("grabber", "") if isinstance(data, dict) else ""
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
