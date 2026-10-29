"""New application entrypoint for the clean rebuild."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .core.lifecycle import lifespan
from .core.log_events import log_animeworld_error, log_debug
from .core.logging import configure_logging, get_logger
from .integrations.animeworld_access import AnimeWorldAccessError
from .web.docs import API_DESCRIPTION, OPENAPI_TAGS
from .web.routes import api_router


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="AnimeWorld Companion",
        description=API_DESCRIPTION,
        version="2.0",
        summary="AnimeWorld bridge for Sonarr, Radarr, downloads, automap, and Torznab.",
        openapi_tags=OPENAPI_TAGS,
        contact={
            "name": "AnimeWorld Companion",
            "url": "https://github.com/r0bb10/AnimeWorld-Companion",
        },
        lifespan=lifespan,
    )
    app.include_router(api_router)

    @app.exception_handler(AnimeWorldAccessError)
    async def animeworld_access_error(request: Request, exc: AnimeWorldAccessError) -> JSONResponse:
        log_animeworld_error(
            get_logger("routes"), "animeworld.request.failed", "AnimeWorld request failed", exc,
            details={"route": request.url.path},
        )
        return JSONResponse(status_code=503, content={"detail": str(exc), "upstream": exc.details()})

    @app.get(
        "/health",
        tags=["System"],
        summary="Liveness probe",
        description="Simple plaintext health endpoint for container checks and reverse proxies.",
    )
    def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    logger = get_logger(__name__)
    log_debug(logger, "app.initialized", "Clean rebuild app initialized")
    return app


app = create_app()
