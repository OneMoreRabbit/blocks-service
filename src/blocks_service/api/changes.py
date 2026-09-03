"""Incremental synchronization.

The outbox is already an ordered, tenant-scoped, append-only record of everything
that happened, written in the same transaction as the change itself. That is
exactly what an offline client needs to catch up on, so it is served directly
rather than duplicated into a second change table that could disagree with it.

Clients poll with the last `eventId` they processed and get everything after it.
Events are notifications: they carry identifiers and the minimum needed to act,
and the client re-reads the resource for the rest.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from .. import errors
from ..dispatcher import envelope_for
from .deps import TenantContext, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/changes", tags=["sync"])


class EventEnvelopeOut(BaseModel):
    """The accepted 0.1 envelope, typed.

    Published as fields rather than a bare object so a generated client gets the
    stable parts — everything a consumer routes and deduplicates on. `payload`
    stays open on purpose: its shape is chosen by `eventType` + `eventVersion`
    and is versioned separately in the event schemas, so freezing it here would
    make every new event type a breaking change to this response.
    """

    eventId: str = Field(description="Monotonic; stable across redeliveries. Deduplicate on this.")
    eventType: str = Field(description="Dotted name, e.g. `session.parked`.")
    eventVersion: str = Field(description="MAJOR.MINOR of the payload schema, as written.")
    tenantId: UUID
    occurredAt: datetime = Field(description="When the originating transaction committed.")
    payload: dict = Field(
        description=(
            "Identifiers and the minimum needed to act. Shape is given by "
            "`eventType` and `eventVersion`; tolerate unknown fields."
        )
    )


class ChangesOut(BaseModel):
    events: list[EventEnvelopeOut]
    next_since: str | None = Field(description="Pass as `since` on the next call.")
    has_more: bool


@router.get(
    "",
    response_model=ChangesOut,
    responses=errors.error_responses(400, 401, 403, 426),
    summary="Events since a cursor",
)
async def changes(
    ctx: TenantContext = Depends(tenant_context),
    since: int = Query(0, ge=0, description="The last eventId already processed."),
    limit: int = Query(100, ge=1, le=500),
) -> ChangesOut:
    cur = await ctx.conn.execute(
        "select id, tenant_id, event_type, event_version, payload, occurred_at"
        " from blocks.outbox where id > %s order by id limit %s",
        (since, limit + 1),
    )
    rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ChangesOut(
        events=[envelope_for(row) for row in rows],
        next_since=str(rows[-1]["id"]) if rows else (str(since) if since else None),
        has_more=has_more,
    )
