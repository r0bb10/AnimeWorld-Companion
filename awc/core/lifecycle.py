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
    level_value = str(settings.log_level or "INFO").upper()
    sonarr_enabled = bool(settings.sonarr_url and settings.sonarr_api_key)
    radarr_enabled = bool(settings.radarr_url and settings.radarr_api_key)
    details = {
        "sonarr": "enabled" if sonarr_enabled else "disabled",
        "radarr": "enabled" if radarr_enabled else "disabled",
    }
    lines = [
        f"sonarr={'enabled' if sonarr_enabled else 'disabled'}",
        f"radarr={'enabled' if radarr_enabled else 'disabled'}",
    ]
    if level_value != "INFO":
        details["level"] = level_value
        lines.insert(0, f"level={level_value}")
    log_info(
        logger,
        "lifecycle.start",
        "AWC starting",
        details=details,
        lines=lines,
    )
    init_db()
    start_sse_streams()
    start_background_workers()
    yield
    stop_sse_streams()
    stop_background_workers()
    log_info(logger, "lifecycle.stop", "AWC rebuild foundation stopping")
    shutdown_logging()
