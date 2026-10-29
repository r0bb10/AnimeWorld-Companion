"""Application lifecycle helpers for the clean rebuild."""

from contextlib import asynccontextmanager

from .config import settings
from .log_events import log_info
from .logging import get_logger, shutdown_logging
from ..repositories.schema import init_db
from ..integrations.animeworld_transport import audit, traffic
from ..services.events_service import start_sse_streams, stop_sse_streams
from ..services.background_service import start_background_workers, stop_background_workers
from ..services.download_service import pause_active_downloads

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
    traffic.start()
    audit("animeworld.http.policy", {
        **traffic.snapshot(), "health_cache_seconds": settings.aw_health_cache_seconds,
        "audit_enabled": settings.aw_request_audit_enabled,
        "audit_retention_days": settings.aw_request_audit_retention_days,
    })
    start_sse_streams()
    start_background_workers()
    yield
    stop_sse_streams()
    traffic.stop()
    pause_active_downloads(reason="system")
    stop_background_workers()
    log_info(logger, "lifecycle.stop", "AWC rebuild foundation stopping")
    shutdown_logging()
