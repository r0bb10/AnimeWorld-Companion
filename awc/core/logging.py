"""Logging helpers for the clean rebuild."""

import logging

from .config import settings


def configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(settings.log_level)
        return

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
