"""Application lifecycle helpers for the clean rebuild."""

from contextlib import asynccontextmanager

from .logging import get_logger
from ..repositories.schema import init_db
from ..services.background_service import start_background_workers, stop_background_workers

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app):
    logger.info("AWC rebuild foundation starting")
    init_db()
    start_background_workers()
    yield
    stop_background_workers()
    logger.info("AWC rebuild foundation stopping")
