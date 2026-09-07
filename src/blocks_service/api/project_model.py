"""The 0.3 project surface (ADR-0009): dashboard, project page, notes, parking.

Opening a project is **navigation**. There is no endpoint here that starts,
resumes or closes anything, because selecting a project must never mutate server
state — that is the workflow physical-device UAT rejected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import errors, project_model as pm
from ..idempotency import Idempotency
from .deps import TenantContext, as_http, idempotency, if_match_version, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects", tags=["projects-0.3"])


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class ProjectRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    """A superset of the 0.2 project shape, deliberately.

    0.2 published `{id, tenant_id, name}` on the same path. Serving a *narrower*
    body here would break an installed 0.2 client during its compatibility
    window, so `tenant_id` is retained and everything else is additive — and the
    contract's own rule is that unknown additive fields are ignored safely.

    One behavioural difference a 0.2 client will see: the list now returns active
    projects only. Archiving did not exist in 0.2, so nothing it created can
    disappear; a project only leaves the list once someone archives it in 0.3,
    which is what archiving means.
    """

    id: UUID
    tenant_id: UUID
    name: str
    status: Literal["active", "archived"]
    row_version: int = Field(description="Send back as `If-Match` on the next mutation.")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class NotesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(max_length=100_000, description="May be empty. Saved explicitly.")


class NotesOut(BaseModel):
    project_id: UUID
    body: str
    row_version: int
    updated_at: datetime


class ProjectParkIn(BaseModel):
    """The three answers, all required. A correction supersedes rather than edits."""

    model_config = ConfigDict(extra="forbid")
    where_i_am: str = Field(min_length=1, max_length=2000)
    next_physical_action: str = Field(min_length=1, max_length=2000)
    must_not_forget: str = Field(min_length=1, max_length=2000)
    device_id: UUID | None = None
    supersedes_id: UUID | None = Field(
        default=None, description="Correcting an earlier record in this project."
    )


class ProjectParkingOut(BaseModel):
    """Named apart from 0.2's `ParkingOut` on purpose.

    A model-name collision makes the generator mangle BOTH into
    module-qualified names, which would rename a published 0.2 schema out from
    under a client still inside its compatibility window.
    """

    id: UUID
    project_id: UUID
    where_i_am: str
    next_physical_action: str
    must_not_forget: str
    authored_by_user_id: UUID
    device_id: UUID | None
    supersedes_id: UUID | None
    created_at: datetime


class ProjectPageOut(BaseModel):
    """One read for the whole project page: the project, its notes, its newest park."""

    project: ProjectOut
    notes: NotesOut
    newest_parking: ProjectParkingOut | None


# ---------------------------------------------------------------------------
# Dashboard and lifecycle
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ProjectOut],
    responses=errors.error_responses(400, 401, 403, 426),
    summary="The dashboard — active projects, or archived on request",
)
async def list_projects(
    ctx: TenantContext = Depends(tenant_context),
    status_filter: Literal["active", "archived"] = Query(
        "active", alias="status",
        description="`active` is the dashboard. `archived` is the separate Archive section.",
    ),
) -> list[ProjectOut]:
    return [ProjectOut(**row) for row in await pm.list_projects(ctx.conn, status_filter)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectOut,
    responses=errors.error_responses(400, 401, 403, 409, 426),
    summary="Create a project",
)
async def create_project(
    body: ProjectIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        row = await pm.create_project(ctx.conn, ctx.tenant_id, body.name)
        return ProjectOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.get(
    "/{project_id}",
    response_model=ProjectPageOut,
    responses=errors.error_responses(401, 403, 404, 426),
    summary="The project page — opening it changes nothing",
)
async def project_page(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> ProjectPageOut:
    """A read. Selecting a project is navigation, never a mutation."""
    try:
        project = await pm.get_project(ctx.conn, project_id)
        notes = await pm.get_notes(ctx.conn, project_id)
    except pm.NotFound as exc:
        raise as_http(exc) from exc
    newest = await pm.newest_parking(ctx.conn, project_id)
    return ProjectPageOut(
        project=ProjectOut(**project),
        notes=NotesOut(**notes),
        newest_parking=ProjectParkingOut(**newest) if newest else None,
    )


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Rename a project",
)
async def rename_project(
    project_id: UUID,
    body: ProjectRename,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> ProjectOut:
    try:
        return ProjectOut(
            **await pm.rename_project(
                ctx.conn, ctx.tenant_id, project_id, expected_version, body.name
            )
        )
    except pm.NotFound as exc:
        raise as_http(exc) from exc
    except Exception as exc:
        raise as_http(exc) from exc


@router.post(
    "/{project_id}/archive",
    response_model=ProjectOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Archive — hide from the dashboard, never delete",
)
async def archive_project(
    project_id: UUID,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> ProjectOut:
    try:
        return ProjectOut(
            **await pm.set_project_status(
                ctx.conn, ctx.tenant_id, project_id, expected_version, "archived"
            )
        )
    except Exception as exc:
        raise as_http(exc) from exc


@router.post(
    "/{project_id}/reopen",
    response_model=ProjectOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Reopen — restore to the dashboard",
)
async def reopen_project(
    project_id: UUID,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> ProjectOut:
    try:
        return ProjectOut(
            **await pm.set_project_status(
                ctx.conn, ctx.tenant_id, project_id, expected_version, "active"
            )
        )
    except Exception as exc:
        raise as_http(exc) from exc


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@router.get(
    "/{project_id}/notes",
    response_model=NotesOut,
    responses=errors.error_responses(401, 403, 404, 426),
    summary="Read the project's notes",
)
async def read_notes(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> NotesOut:
    try:
        return NotesOut(**await pm.get_notes(ctx.conn, project_id))
    except pm.NotFound as exc:
        raise as_http(exc) from exc


@router.put(
    "/{project_id}/notes",
    response_model=NotesOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Save the project's notes",
)
async def save_notes(
    project_id: UUID,
    body: NotesIn,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> NotesOut:
    """Explicit save under the version you edited, so two devices conflict visibly."""
    try:
        return NotesOut(
            **await pm.save_notes(
                ctx.conn, ctx.tenant_id, project_id, expected_version, body.body
            )
        )
    except Exception as exc:
        raise as_http(exc) from exc


# ---------------------------------------------------------------------------
# Parking
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/parking",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectParkingOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Add a park message",
)
async def add_parking(
    project_id: UUID,
    body: ProjectParkIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row = await pm.append_parking(
                ctx.conn, ctx.tenant_id, project_id,
                actor_id=ctx.actor_id,
                where_i_am=body.where_i_am,
                next_physical_action=body.next_physical_action,
                must_not_forget=body.must_not_forget,
                device_id=body.device_id,
                supersedes_id=body.supersedes_id,
            )
        except Exception as exc:
            raise as_http(exc) from exc
        return ProjectParkingOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.get(
    "/{project_id}/parking",
    response_model=list[ProjectParkingOut],
    responses=errors.error_responses(401, 403, 404, 426),
    summary="Park messages, newest first",
)
async def list_parking(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[ProjectParkingOut]:
    """Immutable history, newest first — superseded records included.

    A correction appends; nothing is edited away, so the full history stays
    readable and the newest non-superseded record is what the page shows.
    """
    try:
        await pm.get_project(ctx.conn, project_id)
    except pm.NotFound as exc:
        raise as_http(exc) from exc
    return [ProjectParkingOut(**row) for row in await pm.parking_history(ctx.conn, project_id)]
