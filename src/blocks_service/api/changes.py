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
from pydantic import BaseModel

from ..dispatcher import envelope_for
from .deps import TenantContext, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/changes", tags=["sync"])


class ChangesOut(BaseModel):
    events: list[dict]
    next_since: str | None
    has_more: bool


@router.get("", response_model=ChangesOut, summary="Events since a cursor")
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
