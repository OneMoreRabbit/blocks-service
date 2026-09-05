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
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import errors, operating_loop as loop
from ..idempotency import Idempotency
from .deps import (
    TenantContext,
    as_http,
    idempotency,
    if_match_version,
    tenant_context,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/sessions", tags=["sessions"])


class _PickupCommon(BaseModel):
    """The commitment, shared by both ways of picking up."""

    core_outcome: str = Field(
        min_length=1, max_length=500,
        description="Required. Sized below the available window (invariant 3).",
    )
    full_outcome: str | None = Field(default=None, max_length=500)
    polish_outcome: str | None = Field(default=None, max_length=500)
    boundary_at: datetime | None = Field(
        default=None, description="The real-world edge this march ends at."
    )


class PickupByProject(_PickupCommon):
    """Pick up by naming the project directly."""

    model_config = ConfigDict(extra="forbid")
    project_id: UUID


class PickupByTag(_PickupCommon):
    """Pick up by tapping a block, which resolves to its bound project."""

    model_config = ConfigDict(extra="forbid")
    tag_token: str


#: Exactly one selector, enforced by the SHAPE rather than by a validator.
#:
#: `extra="forbid"` on both members is what makes this exact: sending both
#: selectors fails each member (the other field is unexpected), and sending
#: neither fails each member (the required field is missing). The published
#: document carries this as `oneOf`, so a generator enforces it too — `anyOf`,
#: which FastAPI emits for a union by default, would permit both at once and is
#: rewritten in `app._finalise_openapi`.
PickupIn = PickupByProject | PickupByTag


class ParkIn(BaseModel):
    """The three questions. All required — two answers is not a parking record."""

    where_i_am: str = Field(min_length=1, max_length=2000)
    next_physical_action: str = Field(min_length=1, max_length=2000)
    must_not_forget: str = Field(min_length=1, max_length=2000)
    device_id: UUID | None = None


class _CloseCommon(BaseModel):
    interruption: str | None = Field(
        default=None, pattern="^(boundary|hijacked|abandoned|unknown)$",
        description=(
            "Why the block ended. `hijacked` is a normal input, not a failure "
            "(invariant 2) — the vocabulary is deliberately neutral."
        ),
    )


class CloseWithOutcome(_CloseCommon):
    """Close having reached a tier."""

    model_config = ConfigDict(extra="forbid")
    outcome_tier: str = Field(
        pattern="^(core|full|polish)$",
        description="Which tier was actually reached. Evidence, never a score (invariant 8).",
    )


class CloseWithContinuation(_CloseCommon):
    """Close preserving an explicit continuation, atomically.

    For the offline boundary: park and close as **one** operation, under the one
    `If-Match` the client actually knows. Queuing park then close separately
    cannot work offline — the park advances the row version, and the queued close
    was written before that version existed.
    """

    model_config = ConfigDict(extra="forbid")
    where_i_am: str = Field(min_length=1, max_length=2000)
    next_physical_action: str = Field(min_length=1, max_length=2000)
    must_not_forget: str = Field(min_length=1, max_length=2000)
    device_id: UUID | None = None


class CloseOnExistingContinuation(_CloseCommon):
    """Close a session that was ALREADY parked, without inventing a tier.

    A two-form union of "outcome" and "new continuation" would silently drop this
    flow, and it is a real one: park at lunch, decide later the block is over.
    Forcing such a client to re-send the continuation would duplicate the record;
    forcing an `outcome_tier` would make it claim a tier it did not reach, which
    invariant 8 rules out as firmly as invariant 3 rules out preserving nothing.

    Explicit, so it cannot be produced by accident — an empty body still matches
    no member. The server verifies a continuation actually exists and refuses
    with `409` if it does not.
    """

    model_config = ConfigDict(extra="forbid")
    preserve: Literal["existing_continuation"] = Field(
        description="Assert that this session already has a parking record to preserve."
    )


#: Closed union: exactly one form. Invariant 3 lives in the shape — a body
#: matching no member cannot express "close preserving nothing", so that state is
#: unrepresentable rather than merely refused.
CloseIn = CloseWithOutcome | CloseWithContinuation | CloseOnExistingContinuation


class CalibrationOut(BaseModel):
    id: UUID
    planned_tier: str | None
    actual_tier: str | None
    elapsed_seconds: int | None
    interruption: str | None
    restart_latency_seconds: int | None
    created_at: datetime


class ParkingOut(BaseModel):
    id: UUID
    where_i_am: str
    next_physical_action: str
    must_not_forget: str
    sequence: int
    created_at: datetime


class WorkSessionOut(BaseModel):
    """Named `WorkSessionOut`, not `SessionOut`.

    `SessionOut` is already the bootstrap response in the **accepted 0.1**
    contract. Reusing the name collided, and FastAPI disambiguates a collision by
    mangling BOTH into module-qualified names — which would have silently renamed
    an accepted schema out from under a client generating against 0.2.
    """

    id: UUID
    project_id: UUID
    physical_block_id: UUID | None = Field(
        default=None, description="Null when the session was picked up by project."
    )
    state: str = Field(description="`active`, `parked`, `closed`, or `cancelled`.")
    # Non-nullable: a session cannot exist without one (invariant 3). The previous
    # optional typing let a generated client treat it as absent, which it never is.
    core_outcome: str
    full_outcome: str | None = None
    polish_outcome: str | None = None
    boundary_at: datetime | None = Field(
        default=None, description="Null when no real-world boundary was declared."
    )
    row_version: int = Field(description="Send this back as `If-Match` on the next transition.")


class TransitionOut(BaseModel):
    """What every state transition returns: enough to make the next call."""

    id: UUID
    state: str
    row_version: int


class ParkOut(BaseModel):
    session: TransitionOut
    parking: "ParkingOut"


class CloseOut(BaseModel):
    """What a close returns, in both forms."""

    id: UUID
    state: str
    row_version: int = Field(description="The version after closing.")
    parking_record_id: UUID | None = Field(
        default=None,
        description=(
            "Set when the close carried a continuation: the immutable record that "
            "was appended in the same transaction."
        ),
    )


class ReentryOut(BaseModel):
    """Both fields are **required and nullable**, deliberately.

    No-result is `{"session": null, "parking": null}`, never `{}` and never an
    omitted key. A generated client then has one shape to handle rather than
    three, and "absent" cannot be confused with "not sent".
    """

    session: WorkSessionOut | None = Field(
        description=(
            "May be `closed` — an atomic park-and-close preserves a continuation "
            "on a closed session, and `state` is how a client tells that apart "
            "from a session it can resume. Null when there is nothing to re-enter."
        ),
    )
    parking: ParkingOut | None = Field(
        description=(
            "The newest non-superseded record. The pickup view. Null when the "
            "session has no continuation, or when there is no session."
        ),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkSessionOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Pick up: start a work session",
    description=(
        "Provide **exactly one** of `project_id` or `tag_token`. Returns `409` if "
        "you already have an active session — park or close it first."
    ),
)
async def pickup(
    body: PickupByProject | PickupByTag,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        block_id = None
        if isinstance(body, PickupByTag):
            try:
                resolved = await loop.resolve_token(ctx.conn, body.tag_token)
            except loop.DomainError as exc:
                raise as_http(exc) from exc
            block_id = resolved["block_id"]
            project_id = resolved["project_id"]
            if project_id is None:
                raise errors.invalid_request(
                    "That tag resolves to a block with no current project. "
                    "Bind it first, or pick up by project_id."
                )
        else:
            project_id = body.project_id
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
        return WorkSessionOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.get(
    "/current",
    response_model=ReentryOut,
    responses=errors.error_responses(401, 403, 426),
    summary="Re-entry: where was I?",
    description=(
        "The authoritative re-entry surface. Returns, in order: the **active** "
        "session; otherwise the session owning the **globally newest "
        "non-superseded parking record**, whether parked or closed. State does "
        "not outrank recency — a parked session competes on the age of its own "
        "record like everything else. Constitution invariant 4 requires the "
        "newest parking record to be immediately available, so a client never "
        "reconstructs this. Both fields are required and nullable: no result is "
        "`{\"session\": null, \"parking\": null}`."
    ),
)
async def current(ctx: TenantContext = Depends(tenant_context)) -> ReentryOut:
    session = await loop.current_session(ctx.conn, ctx.actor_id)
    if session is None:
        return ReentryOut(session=None, parking=None)
    parking = await loop.newest_parking(ctx.conn, session["id"])
    return ReentryOut(
        session=WorkSessionOut(**session),
        parking=ParkingOut(**parking) if parking else None,
    )


@router.post(
    "/{session_id}/park",
    response_model=ParkOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Park: answer the three questions",
)
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


@router.post(
    "/{session_id}/resume",
    response_model=TransitionOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Resume a parked session",
)
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


@router.post(
    "/{session_id}/close",
    response_model=CloseOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Close a session",
    description=(
        "Send **exactly one** form: an `outcome_tier`; the complete three-field "
        "continuation; or `{'preserve': 'existing_continuation'}` for a session "
        "already parked. The continuation form appends the immutable parking record "
        "and closes the session in **one transaction** under the single `If-Match` "
        "you already hold — so an offline client never has to guess a row version "
        "that did not exist when it queued the intent. A stale version rolls the "
        "whole thing back and writes nothing, not even the parking record."
    ),
)
async def close(
    session_id: UUID,
    body: CloseWithOutcome | CloseWithContinuation | CloseOnExistingContinuation,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        continuation = None
        outcome_tier = None
        if isinstance(body, CloseWithContinuation):
            continuation = {
                "where_i_am": body.where_i_am,
                "next_physical_action": body.next_physical_action,
                "must_not_forget": body.must_not_forget,
                "device_id": body.device_id,
            }
        elif isinstance(body, CloseWithOutcome):
            outcome_tier = body.outcome_tier
        # CloseOnExistingContinuation leaves both None; the domain then verifies a
        # parking record exists and refuses with 409 if it does not.
        try:
            row, record = await loop.close_session(
                ctx.conn, ctx.tenant_id, session_id, expected_version,
                outcome_tier, body.interruption,
                actor_id=ctx.actor_id, continuation=continuation,
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return CloseOut(
            id=row["id"],
            state=row["state"],
            row_version=row["row_version"],
            parking_record_id=record["id"] if record else None,
        ).model_dump(mode="json")

    return await idem.run(status.HTTP_200_OK, produce)


@router.get(
    "/{session_id}/parking",
    response_model=list[ParkingOut],
    responses=errors.error_responses(401, 403, 404, 426),
    summary="The full parking history of a session",
)
async def history(
    session_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[ParkingOut]:
    """Append-only, so this is the whole truth: corrections appear as new records.

    A session with no parking records returns `[]`; a session that does not exist
    returns `404`. The distinction is the point — otherwise a stale id looks like
    a session nobody has parked.
    """
    try:
        await loop.assert_session_exists(ctx.conn, session_id)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return [ParkingOut(**row) for row in await loop.parking_history(ctx.conn, session_id)]


@router.get(
    "/{session_id}/calibration",
    response_model=list[CalibrationOut],
    responses=errors.error_responses(401, 403, 404, 426),
    summary="What this session measured",
)
async def calibration(
    session_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[CalibrationOut]:
    """Raw observations for one session.

    Invariant 8: this returns measurements and no aggregate. There is no
    completion rate, streak, or total here — not because they are withheld, but
    because none is stored. A client that wants to tune something computes from
    these; a client that wants to score someone has nothing to work with.
    """
    try:
        await loop.assert_session_exists(ctx.conn, session_id)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return [
        CalibrationOut(**row) for row in await loop.calibration_for_session(ctx.conn, session_id)
    ]
