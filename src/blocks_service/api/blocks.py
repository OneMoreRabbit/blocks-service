"""Physical blocks and their bindings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import errors, operating_loop as loop
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


class BindingOut(BaseModel):
    id: UUID
    block_id: UUID
    project_id: UUID


class LookupOut(BaseModel):
    block_id: UUID
    label: str
    project_id: UUID | None = Field(
        description="Null when the block is registered but bound to no project."
    )


class LookupIn(BaseModel):
    tag_token: str = Field(min_length=1)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisteredBlockOut,
    responses=errors.error_responses(400, 401, 403, 409, 426),
    summary="Register a physical block",
)
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


@router.get(
    "",
    response_model=list[BlockOut],
    responses=errors.error_responses(401, 403, 426),
    summary="List the tenant's blocks",
)
async def list_blocks(ctx: TenantContext = Depends(tenant_context)) -> list[BlockOut]:
    return [BlockOut(**row) for row in await loop.list_blocks(ctx.conn)]


@router.post(
    "/{block_id}/binding",
    status_code=status.HTTP_201_CREATED,
    response_model=BindingOut,
    responses=errors.error_responses(400, 401, 403, 404, 409, 426),
    summary="Bind a block to a project, or move it to another",
    description=(
        "A block has **at most one current association**. Binding a block that is "
        "already bound ends the previous association and opens a new one — "
        "reassociation is this same call, not a separate operation. History is "
        "never rewritten: work sessions that ran under the previous association "
        "keep pointing at it. A project may hold many blocks."
    ),
)
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


@router.post(
    "/lookup",
    response_model=LookupOut,
    responses=errors.error_responses(400, 401, 403, 404, 426),
    summary="Resolve an NFC tag token to its block and binding",
)
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


@router.delete(
    "/{block_id}/binding",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=errors.error_responses(401, 403, 404, 426),
    summary="Unbind a block from its current project",
    description=(
        "Ends the current association and opens none. The block stays registered "
        "and its tag keeps resolving — to a block with no project — so a physical "
        "block can be taken out of service without being destroyed or silently "
        "re-pointed. Past work sessions are untouched."
    ),
)
async def unbind(block_id: UUID, ctx: TenantContext = Depends(tenant_context)) -> None:
    try:
        await loop.unbind_block(ctx.conn, ctx.tenant_id, block_id)
    except loop.DomainError as exc:
        raise as_http(exc) from exc
