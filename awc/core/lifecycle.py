"""Application lifecycle helpers for the clean rebuild."""

from contextlib import asynccontextmanager

from .config import settings
from .logging import get_logger
from ..repositories.schema import init_db
from ..services.events_service import start_sse_streams, stop_sse_streams
from ..services.background_service import start_background_workers, stop_background_workers

logger = get_logger("lifecycle")


@asynccontextmanager
async def lifespan(app):
    logger.info(
        "AWC starting: level=%s tz=%s sonarr=%s radarr=%s rss=%s sanitizer=%s eligible=%s",
        settings.log_level,
        settings.timezone_name,
        "on" if settings.sonarr_url and settings.sonarr_api_key else "off",
        "on" if settings.radarr_url and settings.radarr_api_key else "off",
        "on" if settings.rss_enabled else "off",
        "on" if settings.sanitizer_enabled else "off",
        "on" if settings.eligible_enabled else "off",
    )
    init_db()
    start_sse_streams()
    start_background_workers()
    yield
    stop_sse_streams()
    stop_background_workers()
    logger.info("AWC rebuild foundation stopping")
