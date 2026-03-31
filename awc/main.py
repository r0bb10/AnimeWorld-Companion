"""New application entrypoint for the clean rebuild."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from .core.lifecycle import lifespan
from .core.logging import configure_logging, get_logger
from .web.routes import api_router


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="AnimeWorld Companion",
        description="Clean rebuild in progress.",
        version="rebuild",
        lifespan=lifespan,
    )
    app.include_router(api_router)

    @app.get("/health", tags=["System"])
    def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    logger = get_logger(__name__)
    logger.debug("Clean rebuild app initialized")
    return app


app = create_app()
