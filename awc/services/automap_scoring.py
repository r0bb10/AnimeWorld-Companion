"""Shared confidence scoring for automap candidates."""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher
import re

TITLE_WEIGHT = 0.35
YEAR_WEIGHT = 0.20
EPISODE_WEIGHT = 0.20
DATE_WEIGHT = 0.20
STATUS_WEIGHT = 0.05
LANGUAGE_WEIGHT = 0.10
CATEGORY_PENALTY = 0.15


def normalize_title_for_comparison(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    noise_words = [
        "season", "part", "the", "and", "or", "of", "a", "an",
        "di", "no", "wa", "wo", "ni", "ga", "to", "de",
        "cour", "cours", "serie", "series", "tv", "ova", "ona", "movie", "film",
    ]
    words = [word for word in text.split() if word and word not in noise_words]
    return " ".join(words).strip()


def title_similarity_score(manager_title: str, manager_alts: list[str], aw_name: str, aw_jtitle: str = "") -> float:
    manager_candidates = []
    for title in [manager_title, *manager_alts]:
        normalized = normalize_title_for_comparison(title)
        if normalized and normalized not in manager_candidates:
            manager_candidates.append(normalized)

    aw_candidates = []
    for title in [aw_name, aw_jtitle]:
        normalized = normalize_title_for_comparison(title)
        if normalized and normalized not in aw_candidates:
            aw_candidates.append(normalized)

    if not manager_candidates or not aw_candidates:
        return 0.0

    best_ratio = 0.0
    for manager_value in manager_candidates:
        for aw_value in aw_candidates:
            best_ratio = max(best_ratio, SequenceMatcher(None, manager_value, aw_value).ratio())
    return min(best_ratio * TITLE_WEIGHT, TITLE_WEIGHT)


def year_match_score(manager_year: int | None, aw_year: int | str | None) -> float:
    if not manager_year or not aw_year:
        return 0.0
    try:
        diff = abs(int(manager_year) - int(aw_year))
    except (TypeError, ValueError):
        return 0.0
    if diff == 0:
        return YEAR_WEIGHT
    if diff == 1:
        return YEAR_WEIGHT * 0.5
    return 0.0


def episode_count_score(
    manager_episode_count: int,
    aw_episode_count: int | None,
    manager_status: str = "",
    aw_status: str = "",
    *,
    manager_has_aired: bool = True,
    aw_is_placeholder: bool = False,
) -> float:
    if not manager_episode_count:
        return 0.0

    manager_ongoing = manager_status.lower() == "continuing"
    aw_ongoing = aw_status.lower() == "in corso"

    if not manager_has_aired and aw_is_placeholder:
        return EPISODE_WEIGHT

    if manager_ongoing and aw_ongoing:
        return 0.10
    if not aw_episode_count:
        return 0.10
    if manager_episode_count == aw_episode_count:
        return EPISODE_WEIGHT

    pct_diff = abs(manager_episode_count - aw_episode_count) / max(manager_episode_count, aw_episode_count)
    if pct_diff <= 0.10:
        return 0.18
    return 0.10


def status_match_score(manager_status: str = "", aw_status: str = "") -> float:
    mapping = {
        ("continuing", "in corso"): STATUS_WEIGHT,
        ("ended", "finito"): STATUS_WEIGHT,
        ("released", "finito"): STATUS_WEIGHT,
    }
    return mapping.get((manager_status.lower(), aw_status.lower()), 0.0)


def parse_italian_date(date_str: str) -> datetime | None:
    if not date_str:
        return None

    months_it = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }

    match = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", date_str.strip())
    if match:
        day, month_str, year = match.groups()
        month = months_it.get(month_str.lower())
        if month:
            try:
                return datetime(int(year), month, int(day), tzinfo=UTC)
            except ValueError:
                return None

    match = re.match(r"(\d{4})", date_str.strip())
    if match:
        return datetime(int(match.group(1)), 1, 1, tzinfo=UTC)

    return None


def air_date_score(manager_first_aired: str = "", aw_release_datetime: datetime | None = None) -> float:
    if not manager_first_aired or not aw_release_datetime:
        return 0.0
    try:
        if "T" in manager_first_aired:
            manager_dt = datetime.fromisoformat(manager_first_aired.replace("Z", "+00:00"))
        else:
            manager_dt = datetime.fromisoformat(manager_first_aired)
    except (TypeError, ValueError):
        return 0.0

    diff_days = abs((manager_dt.date() - aw_release_datetime.date()).days)
    if diff_days <= 30:
        return DATE_WEIGHT
    if diff_days <= 90:
        return DATE_WEIGHT * 0.5
    return 0.0


def language_match_score(candidate: dict, want_dubbed: bool) -> float:
    if "dub" in candidate:
        return LANGUAGE_WEIGHT if bool(candidate["dub"]) == want_dubbed else -LANGUAGE_WEIGHT

    audio = (candidate.get("aw_audio") or candidate.get("Audio") or "").lower()
    if not audio:
        return 0.0
    is_italian = "italiano" in audio or "italian" in audio
    is_japanese = "giapponese" in audio or "japanese" in audio

    if want_dubbed and is_italian:
        return LANGUAGE_WEIGHT
    if not want_dubbed and is_japanese:
        return LANGUAGE_WEIGHT
    if want_dubbed and is_japanese:
        return -LANGUAGE_WEIGHT
    if not want_dubbed and is_italian:
        return -LANGUAGE_WEIGHT
    return 0.0


def category_score(manager_episode_count: int, aw_category: str = "") -> float:
    if aw_category.lower() in {"film", "movie", "ova", "ona"} and manager_episode_count >= 4:
        return -CATEGORY_PENALTY
    return 0.0


def _alternate_titles(payload: dict) -> list[str]:
    titles: list[str] = []
    for item in payload.get("alternate_titles", []):
        if isinstance(item, dict):
            title = item.get("title", "")
        else:
            title = str(item)
        title = title.strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def calculate_show_confidence(show: dict, season: dict, candidate: dict, want_dubbed: bool) -> tuple[float, dict]:
    manager_alts = _alternate_titles(show)
    manager_year = show.get("year")

    season_number = int(season.get("season_number") or 0)
    manager_first_aired = season.get("air_date_start") or (show.get("first_aired") if season_number == 1 else "")

    title = title_similarity_score(show.get("title", ""), manager_alts, candidate.get("aw_title", ""), candidate.get("aw_jtitle", ""))
    year = year_match_score(manager_year, candidate.get("aw_year"))
    episodes = episode_count_score(
        int(season.get("episode_count") or 0),
        candidate.get("aw_episode_count"),
        show.get("status", ""),
        candidate.get("aw_status", ""),
        manager_has_aired=bool(season.get("has_aired", True)),
        aw_is_placeholder=bool(candidate.get("aw_is_placeholder")),
    )
    air_date = air_date_score(manager_first_aired, candidate.get("aw_release_datetime"))
    status = status_match_score(show.get("status", ""), candidate.get("aw_status", ""))
    language = language_match_score(candidate, want_dubbed=want_dubbed)
    category = category_score(int(season.get("episode_count") or 0), candidate.get("aw_category", ""))
    total = title + year + episodes + air_date + status + language + category
    return total, {
        "title_similarity": round(title, 3),
        "year_match": round(year, 3),
        "episode_count": round(episodes, 3),
        "air_date": round(air_date, 3),
        "status_match": round(status, 3),
        "language_match": round(language, 3),
        "category": round(category, 3),
        "preaired_placeholder": bool(not season.get("has_aired", True) and candidate.get("aw_is_placeholder")),
        "total": round(total, 3),
        "passed": total >= 0.0,
    }


def calculate_movie_confidence(movie: dict, candidate: dict, want_dubbed: bool) -> tuple[float, dict]:
    manager_alts = _alternate_titles(movie)
    title_raw = title_similarity_score(movie.get("title", ""), manager_alts, candidate.get("aw_title", ""), candidate.get("aw_jtitle", "")) / TITLE_WEIGHT if TITLE_WEIGHT else 0.0
    year_raw = year_match_score(movie.get("year"), candidate.get("aw_year")) / YEAR_WEIGHT if YEAR_WEIGHT else 0.0
    air_raw = air_date_score(movie.get("first_aired", ""), candidate.get("aw_release_datetime")) / DATE_WEIGHT if DATE_WEIGHT else 0.0
    status_raw = status_match_score(movie.get("status", ""), candidate.get("aw_status", "")) / STATUS_WEIGHT if STATUS_WEIGHT else 0.0
    language = language_match_score(candidate, want_dubbed=want_dubbed)
    total = title_raw * 0.45 + year_raw * 0.30 + air_raw * 0.15 + status_raw * 0.05 + language
    return total, {
        "title": round(title_raw, 3),
        "year": round(year_raw, 3),
        "air_date": round(air_raw, 3),
        "status": round(status_raw, 3),
        "language": round(language, 3),
        "total": round(total, 3),
        "passed": total >= 0.0,
    }
