"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jarvis.api.routes import ALL_ROUTERS
from jarvis.config import Settings, get_settings
from jarvis.core import JarvisCore
from jarvis.db.base import new_id
from jarvis.errors import JarvisError
from jarvis.logging import bind_context, get_logger, reset_context

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


def create_app(
    settings: Settings | None = None,
    *,
    core: JarvisCore | None = None,
    create_schema: bool = False,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.core = core or JarvisCore.build(settings)
        await app.state.core.startup(create_schema=create_schema)
        try:
            yield
        finally:
            await app.state.core.shutdown()

    app = FastAPI(
        title="JARVIS",
        version="0.1.0",
        description="Personal AI operating system — Phase 1 core.",
        lifespan=lifespan,
        # Docs are useful locally and are an information disclosure in
        # production, where they enumerate every endpoint to anything that can
        # reach the port.
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,   # bearer token, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    _register_middleware(app)
    _register_exception_handlers(app)

    for router in ALL_ROUTERS:
        app.include_router(router, prefix="/api")

    _mount_frontend(app)
    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Assign a request id and bind it for the whole request's logs.

        Client-supplied ids are accepted so a UI can correlate, but truncated —
        an unbounded header must not end up in every log line.
        """
        request_id = (request.headers.get("x-request-id") or new_id("http"))[:64]
        tokens = bind_context(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            reset_context(tokens)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # The UI is self-contained: no CDN scripts, no external styles. This is
        # the rule the Phase 0 audit derived from the dashboard's TradingView
        # injection — nothing third-party executes in the origin that holds a
        # credential.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(JarvisError)
    async def jarvis_error_handler(request: Request, exc: JarvisError) -> JSONResponse:
        # ``message`` (which can contain internals) stays in the log;
        # ``to_dict`` returns only the user-facing form.
        log.warning(
            "api_error",
            code=exc.code,
            path=request.url.path,
            detail=exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("api_unhandled", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong on my side.",
                    "retryable": False,
                }
            },
        )


def _mount_frontend(app: FastAPI) -> None:
    """Serve the command center from the same origin as the API.

    Same-origin keeps the CSP tight and means the browser never needs CORS for
    the normal path.
    """
    if not WEB_DIR.is_dir():
        log.warning("frontend_missing", path=str(WEB_DIR))
        return

    assets = WEB_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")


app = create_app  # uvicorn factory target: `uvicorn jarvis.api.app:app --factory`
