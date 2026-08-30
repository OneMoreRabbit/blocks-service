"""Liveness and readiness.

Split deliberately: liveness answers "should this process be restarted", and
must not depend on anything a restart cannot fix. Readiness answers "should
traffic be routed here", and therefore does check the database -- including that
the runtime role is still incapable of bypassing row-level security.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ..db import Database, RlsBypassError
from .deps import get_db

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response, db: Database = Depends(get_db)) -> dict[str, object]:
    checks: dict[str, str] = {}
    ok = True

    try:
        await db.ping()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unavailable: {exc.__class__.__name__}"
        ok = False

    if ok:
        try:
            await db.assert_cannot_bypass_rls()
            checks["tenant_isolation"] = "enforced"
        except RlsBypassError as exc:
            checks["tenant_isolation"] = str(exc)
            ok = False
        except Exception as exc:
            checks["tenant_isolation"] = f"unverified: {exc.__class__.__name__}"
            ok = False

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "unavailable", "checks": checks}
