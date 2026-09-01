"""The map: ordered milestones, no dates.

Constitution invariant 1. There is no date field to send here and none to
receive, which is enforced by the schema having no such column rather than by
this layer refusing one.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import operating_loop as loop
from ..idempotency import Idempotency
from .deps import TenantContext, as_http, idempotency, if_match_version, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects/{project_id}/map", tags=["map"])


class MapItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    after_id: UUID | None = Field(
        default=None, description="Insert immediately after this item. Omit to append."
    )


class MapItemPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = Field(default=None, pattern="^(open|in_progress|done|dropped)$")


class MapItemOut(BaseModel):
    id: UUID
    title: str
    status: str
    sort_key: int
    row_version: int


@router.get("", response_model=list[MapItemOut], summary="Read the project's map")
async def read_map(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[MapItemOut]:
    return [MapItemOut(**row) for row in await loop.list_map(ctx.conn, project_id)]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a milestone")
async def add_item(
    project_id: UUID,
    body: MapItemIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row = await loop.add_map_item(
                ctx.conn, ctx.tenant_id, project_id, body.title, body.after_id
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return MapItemOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.patch("/{item_id}", response_model=MapItemOut, summary="Update a milestone")
async def update_item(
    item_id: UUID,
    body: MapItemPatch,
    ctx: TenantContext = Depends(tenant_context),
    expected_version: int = Depends(if_match_version),
) -> MapItemOut:
    try:
        row = await loop.update_map_item(
            ctx.conn, item_id, expected_version, title=body.title, status=body.status
        )
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return MapItemOut(**row)


class DependencyIn(BaseModel):
    predecessor_id: UUID
    successor_id: UUID


class DependencyOut(BaseModel):
    predecessor_id: UUID
    successor_id: UUID


@router.get(
    "/dependencies", response_model=list[DependencyOut], summary="Read the map's dependencies"
)
async def read_dependencies(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[DependencyOut]:
    return [DependencyOut(**row) for row in await loop.list_dependencies(ctx.conn, project_id)]


@router.post(
    "/dependencies",
    status_code=status.HTTP_201_CREATED,
    summary="Declare that one milestone precedes another",
)
async def add_dependency(
    body: DependencyIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    """Refuses anything that would create a cycle.

    Not a validation nicety: a cyclic map is one that can never be ordered, and
    being orderable is the whole job of a map (invariant 1).
    """

    async def produce() -> dict:
        try:
            row = await loop.add_dependency(
                ctx.conn, ctx.tenant_id, body.predecessor_id, body.successor_id
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return DependencyOut(**row).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.delete(
    "/dependencies",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a dependency",
)
async def remove_dependency(
    predecessor_id: UUID, successor_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> None:
    try:
        await loop.remove_dependency(ctx.conn, predecessor_id, successor_id)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
