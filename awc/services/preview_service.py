"""Preview helpers that combine resolution and naming."""

from ..domain.media import MediaKind, MediaManager, NamingContext
from .naming_service import build_release_name
from .query_service import resolve_local_query


def build_naming_preview(query: str, media: str = "show") -> dict:
    resolved = resolve_local_query(query, media=media)
    result = resolved.get("result")

    if not result:
        return {
            "matched": False,
            "parsed": resolved["parsed"],
            "filename": None,
            "result": None,
        }

    parsed = resolved["parsed"]
    if media == "movie":
        context = NamingContext(
            manager=MediaManager.RADARR,
            kind=MediaKind.MOVIE,
            title=result["title"],
            year=result.get("year"),
        )
    else:
        context = NamingContext(
            manager=MediaManager.SONARR,
            kind=MediaKind.SERIES,
            title=result["title"],
            season_number=parsed.get("season"),
            episode_number=parsed.get("episode"),
        )

    return {
        "matched": True,
        "parsed": parsed,
        "filename": build_release_name(context),
        "result": result,
    }
