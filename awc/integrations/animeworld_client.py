"""AnimeWorld client foundation for the clean rebuild."""

from dataclasses import dataclass
import requests

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
