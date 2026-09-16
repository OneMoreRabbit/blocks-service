"""Application factory."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import (
    blocks,
    changes,
    health,
    map as map_api,
    project_items,
    project_model,
    session,
    sessions,
)
from .api.deps import require_supported_client
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

    # Everything below is tenant-scoped and version-gated. Health and session
    # bootstrap are not: a client that is too old must still be able to reach a
    # readiness probe and learn *why* it is being refused.
    gated = [Depends(require_supported_client)]
    # 0.3 replaces 0.2's project endpoints on the same paths. Its response is a
    # superset of 0.2's, so an installed 0.2 client keeps working through the
    # declared compatibility window (ADR-0009).
    app.include_router(project_model.router, dependencies=gated)
    app.include_router(project_items.router, dependencies=gated)
    app.include_router(blocks.router, dependencies=gated)
    app.include_router(map_api.router, dependencies=gated)
    app.include_router(sessions.router, dependencies=gated)
    app.include_router(changes.router, dependencies=gated)
    _finalise_openapi(app, settings)
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


#: Routes reachable without a bearer token. Deliberately only the probes: a
#: client too old or unauthenticated must still be able to ask whether the
#: service is up, and learn why it is being refused.
UNAUTHENTICATED_PATHS = frozenset({"/health/live", "/health/ready"})


def _finalise_openapi(app: FastAPI, settings: Settings) -> None:
    """Make the generated description match what is actually served.

    FastAPI's default document is wrong for this API in four ways, each of which
    would mislead a generated client rather than merely annoy a reader:

    - it advertises 422 on every validating route; this service returns 400;
    - it declares no authentication, though every route but the probes requires
      a bearer token — a generated client would have no way to send one;
    - a union request body becomes `anyOf`, which permits BOTH members. Pickup
      means exactly one, so it is published as `oneOf`;
    - the idempotent-replay response header is invisible.

    Rewriting the generated document keeps the artefact honest without hand-
    maintaining a spec beside the code.
    """

    def build() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=settings.api_version,
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
        # Authentication. Bearer everywhere except the probes; `/v1/session`
        # included, since it is where an identity token is exchanged, not where
        # one becomes unnecessary.
        schema["components"].setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "An identity token from the configured OIDC issuer. Verified for "
                "signature, expiry, issuer and audience; never issued by this service."
            ),
        }
        schema["security"] = [{"bearerAuth": []}]
        for path, operations in schema.get("paths", {}).items():
            for operation in operations.values():
                if path in UNAUTHENTICATED_PATHS:
                    operation["security"] = []

        # `anyOf` would permit both pickup selectors at once, which is exactly
        # what the contract forbids. The models already reject it; say so in the
        # published shape too, so a generator enforces it rather than a server.
        for union_path in (
            "/v1/tenants/{tenant_id}/sessions",
            "/v1/tenants/{tenant_id}/sessions/{session_id}/close",
            # Block registration is the same shape of promise: the 0.4
            # server-minted form and the ADR-0014 client-supplied form are a
            # closed choice, never a merge. `anyOf` would let a generator offer a
            # body carrying a client_id under the 120-character legacy limit,
            # which no handler accepts.
            "/v1/tenants/{tenant_id}/blocks",
        ):
            operation = schema["paths"].get(union_path, {}).get("post")
            if not operation:
                continue
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            if "anyOf" in (body.get("schema") or {}):
                body["schema"]["oneOf"] = body["schema"].pop("anyOf")
            # The two results are a closed choice too: which one arrives is
            # decided by which form was sent, and only one can be right.
            for code, response in operation.get("responses", {}).items():
                if not code.startswith("2"):
                    continue
                rs = response.get("content", {}).get("application/json", {}).get("schema") or {}
                if "anyOf" in rs:
                    rs["oneOf"] = rs.pop("anyOf")

        # Idempotent replays are indistinguishable from first responses without
        # this header, and a client that cannot tell cannot report accurately.
        for path, operations in schema.get("paths", {}).items():
            for operation in operations.values():
                if any(
                    p.get("name") == "Idempotency-Key" for p in operation.get("parameters", [])
                ):
                    for code, response in operation.get("responses", {}).items():
                        if code.startswith("2"):
                            response.setdefault("headers", {})["Idempotent-Replay"] = {
                                "description": (
                                    "`true` when this response was replayed from a "
                                    "previously stored outcome rather than executed."
                                ),
                                "schema": {"type": "string", "enum": ["true"]},
                            }

        app.openapi_schema = schema
        return schema

    app.openapi = build
