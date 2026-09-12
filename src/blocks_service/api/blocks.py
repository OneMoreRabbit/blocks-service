"""Physical blocks and their bindings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import errors, operating_loop as loop
from ..idempotency import Idempotency
from .deps import TenantContext, as_http, idempotency, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/blocks", tags=["blocks"])


class BlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    client_id: UUID | None = Field(
        default=None,
        description=(
            "**Optional client identifier, unique within your tenant.** A local-first "
            "client mints this before it programs the tag and always supplies it, so a "
            "registration replayed after any offline period cannot duplicate. It is "
            "**not** the canonical id: Service still generates that and returns both. "
            "Supplied together with `tag_token` or not at all. Two tenants may use the "
            "same value for unrelated blocks, and neither can learn anything about the "
            "other. Omit both to keep API 0.4 behaviour."
        ),
    )
    tag_token: str | None = Field(
        default=None,
        description=(
            "**Optional client-generated tag token**, exactly the canonical "
            "ADR-0014 value: 32 random bytes as 43 characters of unpadded base64url. "
            "Send the token only -- never the whole `blocks://tag/v1/<token>` URI. "
            "Supply it when the tag was already programmed offline, so the value "
            "physically written to the tag is the one Service records. Service stores "
            "only its hash and echoes the token back solely in this registration's own "
            "result. Supplied together with `client_id` or not at all."
        ),
    )

    @model_validator(mode="after")
    def _identity_is_all_or_nothing(self) -> "BlockIn":
        # Half an identity cannot replay and cannot conflict. Refusing it here
        # makes the contract state the rule; the 0012 check constraint makes the
        # database refuse to hold the state at all.
        if (self.client_id is None) != (self.tag_token is None):
            raise ValueError("client_id and tag_token are supplied together or both omitted")
        return self


class BlockOut(BaseModel):
    id: UUID
    label: str
    state: str
    bound_project_id: UUID | None = None


class RegisteredBlockOut(BlockOut):
    client_id: UUID | None = Field(
        default=None,
        description="Echoed when you supplied one, so a client can attribute this result.",
    )
    tag_token: str = Field(
        description=(
            "The token for the NFC tag. Returned ONCE, in this registration's own "
            "result; the service stores only its hash and cannot show it again. When "
            "you supplied the token, this is that same value echoed back -- Service "
            "never replaces a token a tag already carries."
        )
    )


class ReplayedBlockOut(BlockOut):
    """A registration recognised by resource identity after the result expired.

    No `tag_token`. The client that supplied one already has it, and a resource
    replay is a read of current state rather than the original result -- Service
    could only reconstruct the token by storing it, which it deliberately does not.
    """

    client_id: UUID | None = None


class _ResourceReplay(Exception):
    """A registration recognised by RESOURCE identity, not by a stored result.

    `200` rather than `201`: nothing was created by this request. Distinguished
    from an ordinary idempotent replay because that one returns the original bytes
    and this one can only return current state.
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
        try:
            block, token, replayed = await loop.register_block(
                ctx.conn,
                ctx.tenant_id,
                body.label,
                client_id=body.client_id,
                token=body.tag_token,
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        if replayed:
            raise _ResourceReplay(
                ReplayedBlockOut(
                    id=block.id,
                    label=block.label,
                    state=block.state,
                    client_id=body.client_id,
                ).model_dump(mode="json")
            )
        return RegisteredBlockOut(
            id=block.id,
            label=block.label,
            state=block.state,
            client_id=body.client_id,
            tag_token=token,
        ).model_dump(mode="json")

    try:
        return await idem.run(status.HTTP_201_CREATED, produce)
    except _ResourceReplay as replay:
        # This request holds the key reservation and is finishing without going
        # back through run(), so it records its own outcome -- otherwise the
        # reservation commits with no result and the next retry cannot replay it.
        await idem.complete(status.HTTP_200_OK, replay.body)
        return replay.response()


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
