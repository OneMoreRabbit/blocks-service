"""Work sessions: pick up, park, resume, close — and re-entry.

The operating loop the whole product exists for. Two constitutional invariants
show up directly in this file:

  Invariant 3 -- a session commits to a core outcome, and parking is a
    transition that always carries its record. There is no way to leave a
    session half-finished and silent.
  Invariant 4 -- the parking contract is exactly three questions, all required.
    `GET /current` is re-entry: it hands back the newest record without the
    caller having to know what state they left things in.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import operating_loop as loop
from ..idempotency import Idempotency
from .deps import (
    TenantContext,
    as_http,
    idempotency,
    if_match_version,
    tenant_context,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/sessions", tags=["sessions"])


class PickupIn(BaseModel):
    project_id: UUID | None = Field(default=None, description="Pick up by project.")
    tag_token: str | None = Field(default=None, description="Or pick up by NFC tag.")
    core_outcome: str = Field(
        min_length=1, max_length=500,
        description="Required. Sized below the available window (invariant 3).",
    )
    full_outcome: str | None = Field(default=None, max_length=500)
    polish_outcome: str | None = Field(default=None, max_length=500)
    boundary_at: datetime | None = Field(
        default=None, description="The real-world edge this march ends at."
    )


class ParkIn(BaseModel):
    """The three questions. All required — two answers is not a parking record."""

    where_i_am: str = Field(min_length=1, max_length=2000)
    next_physical_action: str = Field(min_length=1, max_length=2000)
    must_not_forget: str = Field(min_length=1, max_length=2000)
    device_id: UUID | None = None


class CloseIn(BaseModel):
    outcome_tier: str | None = Field(
        default=None, pattern="^(core|full|polish)$",
        description="Calibration evidence, never a score (invariant 8).",
    )


class ParkingOut(BaseModel):
    id: UUID
    where_i_am: str
    next_physical_action: str
    must_not_forget: str
    sequence: int
    created_at: datetime


class SessionOut(BaseModel):
    id: UUID
    project_id: UUID
    physical_block_id: UUID | None = None
    state: str
    core_outcome: str | None = None
    full_outcome: str | None = None
    polish_outcome: str | None = None
    boundary_at: datetime | None = None
    row_version: int


class ReentryOut(BaseModel):
    session: SessionOut | None
    parking: ParkingOut | None = Field(
        default=None, description="The newest non-superseded record. The pickup view."
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Pick up: start a work session")
async def pickup(
    body: PickupIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        project_id, block_id = body.project_id, None
        if body.tag_token:
            try:
                resolved = await loop.resolve_token(ctx.conn, body.tag_token)
            except loop.DomainError as exc:
                raise as_http(exc) from exc
            block_id = resolved["block_id"]
            project_id = project_id or resolved["project_id"]
        if project_id is None:
            from .. import errors

            raise errors.invalid_request(
                "Provide project_id, or a tag_token for a block bound to a project."
            )
        try:
            row = await loop.start_session(
                ctx.conn,
                ctx.tenant_id,
                project_id=project_id,
                actor_id=ctx.actor_id,
                core_outcome=body.core_outcome,
                full_outcome=body.full_outcome,
                polish_outcome=body.polish_outcome,
                boundary_at=body.boundary_at,
                physical_block_id=block_id,
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return SessionOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.get("/current", response_model=ReentryOut, summary="Re-entry: where was I?")
async def current(ctx: TenantContext = Depends(tenant_context)) -> ReentryOut:
    session = await loop.current_session(ctx.conn, ctx.actor_id)
    if session is None:
        return ReentryOut(session=None)
    parking = await loop.newest_parking(ctx.conn, session["id"])
    return ReentryOut(
        session=SessionOut(**session),
        parking=ParkingOut(**parking) if parking else None,
    )


@router.post("/{session_id}/park", summary="Park: answer the three questions")
async def park(
    session_id: UUID,
    body: ParkIn,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            session, record = await loop.park_session(
                ctx.conn,
                ctx.tenant_id,
                session_id,
                expected_version,
                actor_id=ctx.actor_id,
                where_i_am=body.where_i_am,
                next_physical_action=body.next_physical_action,
                must_not_forget=body.must_not_forget,
                device_id=body.device_id,
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return {
            "session": {
                "id": str(session["id"]),
                "state": session["state"],
                "row_version": session["row_version"],
            },
            "parking": ParkingOut(**record).model_dump(mode="json"),
        }

    return await idem.run(status.HTTP_200_OK, produce)


@router.post("/{session_id}/resume", summary="Resume a parked session")
async def resume(
    session_id: UUID,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await loop.resume_session(ctx.conn, ctx.tenant_id, session_id, expected_version)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return {"id": str(row["id"]), "state": row["state"], "row_version": row["row_version"]}


@router.post("/{session_id}/close", summary="Close a session")
async def close(
    session_id: UUID,
    body: CloseIn,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await loop.close_session(
            ctx.conn, ctx.tenant_id, session_id, expected_version, body.outcome_tier
        )
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return {"id": str(row["id"]), "state": row["state"], "row_version": row["row_version"]}


@router.get(
    "/{session_id}/parking",
    response_model=list[ParkingOut],
    summary="The full parking history of a session",
)
async def history(
    session_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[ParkingOut]:
    """Append-only, so this is the whole truth: corrections appear as new records."""
    return [ParkingOut(**row) for row in await loop.parking_history(ctx.conn, session_id)]
