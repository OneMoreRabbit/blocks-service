"""Physical blocks and their bindings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import operating_loop as loop
from ..idempotency import Idempotency
from .deps import TenantContext, as_http, idempotency, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/blocks", tags=["blocks"])


class BlockIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class BlockOut(BaseModel):
    id: UUID
    label: str
    state: str
    bound_project_id: UUID | None = None


class RegisteredBlockOut(BlockOut):
    tag_token: str = Field(
        description=(
            "The token to write to the NFC tag. Returned ONCE, at registration; "
            "the service stores only its hash and cannot show it again."
        )
    )


class BindIn(BaseModel):
    project_id: UUID


class LookupIn(BaseModel):
    tag_token: str = Field(min_length=1)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register a physical block")
async def register(
    body: BlockIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        block, token = await loop.register_block(ctx.conn, ctx.tenant_id, body.label)
        return RegisteredBlockOut(
            id=block.id, label=block.label, state=block.state, tag_token=token
        ).model_dump(mode="json")

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.get("", response_model=list[BlockOut], summary="List the tenant's blocks")
async def list_blocks(ctx: TenantContext = Depends(tenant_context)) -> list[BlockOut]:
    return [BlockOut(**row) for row in await loop.list_blocks(ctx.conn)]


@router.post("/{block_id}/binding", summary="Bind a block to a project")
async def bind(
    block_id: UUID,
    body: BindIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row = await loop.bind_block(ctx.conn, ctx.tenant_id, block_id, body.project_id)
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        return {
            "id": str(row["id"]),
            "block_id": str(row["block_id"]),
            "project_id": str(row["project_id"]),
        }

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.post("/lookup", summary="Resolve an NFC tag token to its block and binding")
async def lookup(body: LookupIn, ctx: TenantContext = Depends(tenant_context)) -> dict:
    """A POST because the token belongs in a body, not in a URL that gets logged."""
    try:
        row = await loop.resolve_token(ctx.conn, body.tag_token)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
    return {
        "block_id": str(row["block_id"]),
        "label": row["label"],
        "project_id": str(row["project_id"]) if row["project_id"] else None,
    }
