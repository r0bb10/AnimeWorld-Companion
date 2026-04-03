"""Application lifecycle helpers for the clean rebuild."""

from contextlib import asynccontextmanager

from .config import settings
from .log_events import log_info
from .logging import get_logger, shutdown_logging
from ..repositories.schema import init_db
from ..services.events_service import start_sse_streams, stop_sse_streams
from ..services.background_service import start_background_workers, stop_background_workers

logger = get_logger("lifecycle")


@asynccontextmanager
async def lifespan(app):
    log_info(
        logger,
        "lifecycle.start",
        "AWC starting",
        details={
            "level": settings.log_level,
            "tz": settings.timezone_name,
            "sonarr": "on" if settings.sonarr_url and settings.sonarr_api_key else "off",
            "radarr": "on" if settings.radarr_url and settings.radarr_api_key else "off",
            "rss": "on" if settings.rss_enabled else "off",
            "sanitizer": "on" if settings.sanitizer_enabled else "off",
            "eligible": "on" if settings.eligible_enabled else "off",
        },
        lines=[
            f"level={settings.log_level}",
            f"tz={settings.timezone_name}",
            f"sonarr={'on' if settings.sonarr_url and settings.sonarr_api_key else 'off'}",
            f"radarr={'on' if settings.radarr_url and settings.radarr_api_key else 'off'}",
            f"rss={'on' if settings.rss_enabled else 'off'}",
            f"sanitizer={'on' if settings.sanitizer_enabled else 'off'}",
            f"eligible={'on' if settings.eligible_enabled else 'off'}",
        ],
    )
    init_db()
    start_sse_streams()
    start_background_workers()
    yield
    stop_sse_streams()
    stop_background_workers()
    log_info(logger, "lifecycle.stop", "AWC rebuild foundation stopping")
    shutdown_logging()
