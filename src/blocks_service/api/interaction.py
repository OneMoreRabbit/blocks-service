"""API 0.7 convergence surface for ordered work and Park tombstones."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import errors, interaction
from .deps import TenantContext, as_http, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["interaction-sync"])


class ReorderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    aggregate_id: UUID
    base_version: int = Field(ge=1)
    ordered_client_ids: list[UUID]


class OrderOut(BaseModel):
    operation_id: UUID | None
    aggregate_id: UUID
    project_id: UUID | None = None
    ordered_client_ids: list[UUID]
    version: int


class DeleteParkingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    aggregate_id: UUID
    target_id: UUID
    base_version: int = Field(ge=1)


class ParkingDeleteOut(BaseModel):
    operation_id: UUID
    aggregate_id: UUID
    project_id: UUID
    target_id: UUID
    version: int
    deleted_at: datetime | None
    changed: bool


class ParkingTombstoneOut(BaseModel):
    operation_id: UUID
    project_id: UUID
    target_id: UUID
    version: int
    deleted_at: datetime


MUTATION_ERRORS = errors.error_responses(400, 401, 403, 404, 409, 426)
READ_ERRORS = errors.error_responses(401, 403, 404, 426)


def _response(result: dict, replayed: bool) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=result,
        headers={"Idempotent-Replay": "true"} if replayed else {},
    )


@router.get("/interaction/project-order", response_model=OrderOut, responses=READ_ERRORS)
async def read_project_order(
    ctx: TenantContext = Depends(tenant_context),
) -> dict:
    try:
        return await interaction.project_order(ctx.conn, ctx.tenant_id)
    except interaction.DomainError as exc:
        raise as_http(exc) from exc


@router.put(
    "/interaction/project-order", response_model=OrderOut, responses=MUTATION_ERRORS,
    summary="Replace the complete active-Project custom order",
)
async def reorder_projects(
    body: ReorderIn, ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    try:
        result, replayed = await interaction.reorder_projects(
            ctx.conn, ctx.tenant_id,
            operation_id=body.operation_id, aggregate_id=body.aggregate_id,
            base_version=body.base_version, ordered_client_ids=body.ordered_client_ids,
        )
    except interaction.DomainError as exc:
        raise as_http(exc) from exc
    return _response(result, replayed)


@router.get(
    "/projects/{project_id}/interaction/link-order",
    response_model=OrderOut, responses=READ_ERRORS,
)
async def read_link_order(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context),
) -> dict:
    try:
        return await interaction.link_order(ctx.conn, ctx.tenant_id, project_id)
    except interaction.DomainError as exc:
        raise as_http(exc) from exc


@router.put(
    "/projects/{project_id}/interaction/link-order",
    response_model=OrderOut, responses=MUTATION_ERRORS,
    summary="Replace the complete live-Link order for one Project",
)
async def reorder_links(
    project_id: UUID, body: ReorderIn,
    ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    try:
        result, replayed = await interaction.reorder_links(
            ctx.conn, ctx.tenant_id, project_id,
            operation_id=body.operation_id, aggregate_id=body.aggregate_id,
            base_version=body.base_version, ordered_client_ids=body.ordered_client_ids,
        )
    except interaction.DomainError as exc:
        raise as_http(exc) from exc
    return _response(result, replayed)


@router.post(
    "/projects/{project_id}/parking/{parking_id}/delete",
    response_model=ParkingDeleteOut, responses=MUTATION_ERRORS,
    summary="Tombstone one immutable Park record",
)
async def delete_parking(
    project_id: UUID, parking_id: UUID, body: DeleteParkingIn,
    ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    try:
        result, replayed = await interaction.delete_parking(
            ctx.conn, ctx.tenant_id, project_id, parking_id,
            operation_id=body.operation_id, aggregate_id=body.aggregate_id,
            target_id=body.target_id, base_version=body.base_version,
        )
    except interaction.DomainError as exc:
        raise as_http(exc) from exc
    return _response(result, replayed)


@router.get(
    "/projects/{project_id}/interaction/parking-tombstones",
    response_model=list[ParkingTombstoneOut], responses=READ_ERRORS,
    summary="Park tombstones retained for synchronization",
)
async def read_parking_tombstones(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context),
) -> list[dict]:
    try:
        await interaction.link_order(ctx.conn, ctx.tenant_id, project_id)
        return await interaction.parking_tombstones(ctx.conn, project_id)
    except interaction.DomainError as exc:
        raise as_http(exc) from exc
