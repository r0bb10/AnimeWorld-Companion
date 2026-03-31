"""Application lifecycle helpers for the clean rebuild."""

from contextlib import asynccontextmanager

from .logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app):
    logger.info("AWC rebuild foundation starting")
    yield
    logger.info("AWC rebuild foundation stopping")
