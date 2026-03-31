"""AnimeWorld client for search, episode discovery, and file resolution."""

from dataclasses import dataclass
import re
import requests
from bs4 import BeautifulSoup

from ..core.config import settings


@dataclass(frozen=True)
class AnimeWorldHealth:
    ok: bool
    url: str


class AnimeWorldClient:
    """AnimeWorld client with light session/bootstrap support."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.aw_base_url).rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"
        self._csrf_token = ""

    def _bootstrap(self) -> None:
        response = self.session.get(self.base_url, timeout=15)
        response.raise_for_status()
        match = re.search(r'<meta[^>]+id="csrf-token"[^>]+content="([^"]+)"', response.text)
        if match:
            self._csrf_token = match.group(1)
            self.session.headers["csrf-token"] = self._csrf_token

    def health(self) -> AnimeWorldHealth:
        if not self.base_url:
            return AnimeWorldHealth(ok=False, url="")
        return AnimeWorldHealth(ok=True, url=self.base_url)

    def slug_to_url(self, slug: str) -> str:
        return f"{self.base_url}/play/{slug.strip('/')}/"

    def url_to_slug(self, url: str) -> str:
        if not url:
            return ""
        if "/play/" in url:
            return url.split("/play/", 1)[1].split("/")[0]
        return url.strip("/")

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if not self.base_url or not query.strip():
            return []
        try:
            response = self.session.get(
                f"{self.base_url}/search",
                params={"keyword": query.strip()},
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except requests.RequestException:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for item in soup.select(".film-list .item")[:limit]:
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
                    "slug": href.split("/play/")[-1] if "/play/" in href else "",
                    "poster": image.get("src", "") if image else "",
                    "kind": status.get_text(" ", strip=True) if status else "",
                }
            )
        return results

    def get_episodes(self, slug_or_url: str) -> list[dict]:
        target = slug_or_url if slug_or_url.startswith("http") else self.slug_to_url(slug_or_url)
        try:
            response = self.session.get(target, timeout=15)
            response.raise_for_status()
        except requests.RequestException:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        episodes = []
        for anchor in soup.select("li.episode a[data-episode-num]"):
            episodes.append(
                {
                    "number": anchor.get("data-episode-num") or anchor.get("data-num") or "",
                    "episode_id": anchor.get("data-episode-id") or anchor.get("data-id") or "",
                    "href": anchor.get("href", ""),
                }
            )
        return episodes

    def get_file_info(self, episode_id: str) -> list[dict]:
        if not episode_id:
            return []
        if not self._csrf_token:
            try:
                self._bootstrap()
            except requests.RequestException:
                return []
        try:
            response = self.session.post(f"{self.base_url}/api/download/{episode_id}", timeout=15)
            if response.status_code in (401, 403):
                self._bootstrap()
                response = self.session.post(f"{self.base_url}/api/download/{episode_id}", timeout=15)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
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
            head = self.session.head(cdn_url, timeout=8, allow_redirects=True)
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

    def count_non_special_episodes(self, episodes: list[dict]) -> tuple[int, int, int]:
        total = len(episodes)
        non_special = 0
        for item in episodes:
            number = item.get("number", "")
            try:
                if float(number).is_integer():
                    non_special += 1
            except Exception:
                non_special += 1
        return non_special, total, total - non_special
