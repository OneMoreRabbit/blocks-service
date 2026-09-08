"""The project surface (ADR-0009, extended by ADR-0011): dashboard, page, notes, parking.

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

#: The `200` a resource replay returns, declared so a generated client never has
#: to infer it. The distinction from `201` is real: `201` carries the ORIGINAL
#: STORED BYTES of the first response, while `200` carries the resource's CURRENT
#: state, which may have been renamed or superseded since it was created.
def _replay_responses(model: str) -> dict:
    return {
        200: {
            "description": (
                "Resource replay. The ordinary idempotency result had expired, so "
                "the request was recognised by tenant-scoped client identity "
                "instead. The body is the resource's CURRENT state, not the "
                "original stored response, and nothing was created."
            ),
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}},
            "headers": {
                "Idempotent-Replay": {
                    "description": "`true`. Always present on any replay.",
                    "schema": {"type": "string", "enum": ["true"]},
                },
                "Idempotent-Replay-Source": {
                    "description": (
                        "`resource` — recognised by client identity after the stored "
                        "result expired. Absent on a retained-result replay, which "
                        "returns 201 with the original bytes."
                    ),
                    "schema": {"type": "string", "enum": ["resource"]},
                },
            },
        }
    }

router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects", tags=["projects"])


class _ResourceReplay(Exception):
    """A create/append recognised by RESOURCE identity rather than a stored result.

    Distinguished from an ordinary idempotent replay in the response, because the
    two carry different guarantees. A retained-result replay returns the original
    bytes; this one can only return the resource's CURRENT state, which may have
    been renamed or superseded since. `200` rather than `201` says so — nothing was
    created by this request — and the header marks it as a replay either way.
    """

    def __init__(self, body: dict) -> None:
        self._body = body

    @property
    def body(self) -> dict:
        return self._body

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=self._body,
            headers={"Idempotent-Replay": "true", "Idempotent-Replay-Source": "resource"},
        )


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: UUID | None = Field(
        default=None,
        description=(
            "**Optional client identifier, unique within your tenant.** A local-first "
            "client mints this at local commit and always supplies it, so a create "
            "replayed after any offline period cannot duplicate. It is **not** the "
            "canonical id: Service still generates that and returns both. Two tenants "
            "may use the same value for unrelated projects, and neither can learn "
            "anything about the other. Omit it to keep API 0.3 behaviour."
        ),
    )
    name: str = Field(min_length=1, max_length=200)


class ProjectRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    client_id: UUID | None = Field(
        default=None, description="Your identifier for this project, if you supplied one."
    )
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
    client_id: UUID | None = Field(
        default=None,
        description="Optional client identifier, unique within your tenant. Not the canonical id.",
    )
    occurred_at: datetime | None = Field(
        default=None,
        description=(
            "When the user committed this park locally, normalized UTC. Supplied by "
            "a local-first client; omitted by older clients, which get the server's "
            "acceptance time. Recorded as given and never silently rewritten — "
            "ordering by it is what stops delayed delivery reordering the user's "
            "own history."
        ),
    )
    where_i_am: str = Field(min_length=1, max_length=2000)
    next_physical_action: str = Field(min_length=1, max_length=2000)
    must_not_forget: str = Field(min_length=1, max_length=2000)
    device_id: UUID | None = None
    supersedes_id: UUID | None = Field(
        default=None, description="Correcting an earlier record in this project."
    )


class ProjectParkingOut(BaseModel):
    client_id: UUID | None = Field(
        default=None, description="Your identifier for this record, if you supplied one."
    )
    occurred_at: datetime = Field(
        description="When the user committed it. Always present from 0.4."
    )
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
    responses={
        **errors.error_responses(400, 401, 403, 409, 426),
        **_replay_responses("ProjectOut"),
    },
    summary="Create a project",
    description=(
        "`201` on first creation, and on a replay while the idempotency result is "
        "retained — that response carries the **original stored bytes**. `200` on a "
        "resource replay after the result expired, carrying the resource's "
        "**current state**. `409` if the same `client_id` names a different "
        "operation in this tenant."
    ),
)
async def create_project(
    body: ProjectIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row, replayed = await pm.create_project(
                ctx.conn, ctx.tenant_id, body.name,
                client_id=body.client_id, fingerprint=idem.creation_identity,
            )
        except pm.DomainError as exc:
            raise as_http(exc) from exc
        if replayed:
            raise _ResourceReplay(ProjectOut(**row).model_dump(mode="json"))
        return ProjectOut(**row).model_dump(mode="json")

    try:
        return await idem.run(status.HTTP_201_CREATED, produce)
    except _ResourceReplay as replay:
        # This request holds the key reservation and is finishing without going
        # back through run(), so it records its own outcome. Otherwise the
        # reservation commits with no result and the next retry cannot replay it.
        await idem.complete(status.HTTP_200_OK, replay.body)
        return replay.response()


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
    responses={
        **errors.error_responses(400, 401, 403, 404, 409, 426),
        **_replay_responses("ProjectParkingOut"),
    },
    summary="Add a park message",
    description=(
        "`201` on first append and on a retained-result replay (original bytes). "
        "`200` on a resource replay after expiry (current state). `409` if the same "
        "`client_id` names a different operation in this tenant."
    ),
)
async def add_parking(
    project_id: UUID,
    body: ProjectParkIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row, replayed = await pm.append_parking(
                ctx.conn, ctx.tenant_id, project_id,
                actor_id=ctx.actor_id,
                where_i_am=body.where_i_am,
                next_physical_action=body.next_physical_action,
                must_not_forget=body.must_not_forget,
                device_id=body.device_id,
                supersedes_id=body.supersedes_id,
                client_id=body.client_id,
                occurred_at=body.occurred_at,
                fingerprint=idem.creation_identity,
            )
        except pm.DomainError as exc:
            raise as_http(exc) from exc
        if replayed:
            raise _ResourceReplay(ProjectParkingOut(**row).model_dump(mode="json"))
        return ProjectParkingOut(**row).model_dump(mode="json")

    try:
        return await idem.run(status.HTTP_201_CREATED, produce)
    except _ResourceReplay as replay:
        # This request holds the key reservation and is finishing without going
        # back through run(), so it records its own outcome. Otherwise the
        # reservation commits with no result and the next retry cannot replay it.
        await idem.complete(status.HTTP_200_OK, replay.body)
        return replay.response()


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
