"""AnimeWorld client foundation for the clean rebuild."""

from dataclasses import dataclass
import requests
from bs4 import BeautifulSoup

from ..core.config import settings


@dataclass(frozen=True)
class AnimeWorldHealth:
    ok: bool
    url: str


class AnimeWorldClient:
    """Small foundation client that will later absorb session/bootstrap logic."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.aw_base_url).rstrip("/")
        self.session = requests.Session()

    def health(self) -> AnimeWorldHealth:
        if not self.base_url:
            return AnimeWorldHealth(ok=False, url="")
        return AnimeWorldHealth(ok=True, url=self.base_url)

    def slug_to_url(self, slug: str) -> str:
        return f"{self.base_url}/play/{slug.strip('/')}/"

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
