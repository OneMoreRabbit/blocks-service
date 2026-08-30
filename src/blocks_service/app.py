"""Application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import health, projects, session
from .config import Settings, get_settings
from .db import Database
from .identity import build_verifier


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(
            settings.app_dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
        await db.open()
        app.state.db = db
        app.state.verifier = build_verifier(settings)
        app.state.settings = settings
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        summary="Authoritative domain, tenancy, and device-coordination API for Blocks.",
        lifespan=lifespan,
    )

    @app.exception_handler(StarletteHTTPException)
    async def _error_envelope(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """One error shape for every failure, including the ones FastAPI raises.

        Without this, `ApiError` returns {"error": ...} while a routing 404
        returns {"detail": ...} and clients need two parsers for one API.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": _code_for(exc.status_code), "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)

    app.include_router(health.router)
    app.include_router(session.router)
    app.include_router(projects.router)
    return app


def _code_for(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "unauthenticated",
        403: "unauthorized",
        404: "not_found",
        409: "conflict",
        426: "upgrade_required",
    }.get(status_code, "invalid_request")
