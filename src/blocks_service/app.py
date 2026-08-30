"""Application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import health, projects, session
from .config import Settings, get_settings
from .errors import ErrorCode
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

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Malformed requests are 400 `invalid_request`, not FastAPI's default 422.

        The published contract lists one code per status and does not include 422.
        Leaving the framework default in place would have meant a client branching
        on the documented codes silently failing to recognise the single most
        common client-side error.
        """
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": _describe(exc.errors()),
                }
            },
        )

    app.include_router(health.router)
    app.include_router(session.router)
    app.include_router(projects.router)
    _publish_error_responses(app)
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


def _describe(errors: list[dict]) -> str:
    """A stable, readable summary of what was wrong with the request.

    Locations only, never values: an error message is echoed into logs and
    client-side reports, and a rejected body can contain anything.
    """
    if not errors:
        return "The request could not be validated."
    parts = []
    for err in errors[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body") or "request"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    suffix = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
    return "; ".join(parts) + suffix


ERROR_SCHEMA = {
    "title": "Error",
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Stable machine-readable code. Branch on this.",
                    "enum": [c.value for c in ErrorCode],
                },
                "message": {
                    "type": "string",
                    "description": "Human-readable prose. May change in a PATCH; do not branch on it.",
                },
            },
        }
    },
}


def _publish_error_responses(app: FastAPI) -> None:
    """Make the generated description match the error semantics actually served.

    FastAPI advertises a 422 on every route that validates input, and describes
    it with its own schema. Both are wrong for this API: validation failures are
    returned as 400 `invalid_request` in the contract envelope. Rewriting the
    generated document keeps the published artefact honest without hand-
    maintaining a spec beside the code.
    """

    def build() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("schemas", {})["Error"] = ERROR_SCHEMA
        error_ref = {
            "description": "Error",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
        }
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                responses = operation.get("responses", {})
                if responses.pop("422", None) is not None:
                    responses["400"] = {**error_ref, "description": "Malformed request"}
        app.openapi_schema = schema
        return schema

    app.openapi = build
