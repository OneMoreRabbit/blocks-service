"""Physical blocks and their bindings."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import errors, operating_loop as loop
from ..idempotency import Idempotency
from .deps import (
    TenantContext,
    as_http,
    idempotency,
    replay_responses,
    tenant_context,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/blocks", tags=["blocks"])


#: A label must contain something. `min_length` alone counts characters, so "   "
#: passed request validation and died on the database's
#: `physical_blocks_label_not_blank` check -- outside the domain-error mapping,
#: so the caller got an internal error instead of the declared 400. The pattern is
#: unanchored on purpose: JSON Schema `pattern` is a search, so `\S` reads as
#: "contains at least one non-whitespace character" to a generator as well.
NON_BLANK = r"\S"


class LegacyBlockIn(BaseModel):
    """API 0.4 form: Service mints the token and returns it once."""

    model_config = ConfigDict(extra="forbid")
    label: str = Field(
        min_length=1,
        max_length=120,
        pattern=NON_BLANK,
        description="Non-blank, up to 120 characters. Whitespace alone is not a label.",
    )


class ClientBlockIn(BaseModel):
    """API 0.5 form: the tag was already programmed offline (ADR-0014).

    `client_id` and `tag_token` are **both** required here. That is what makes the
    two forms a closed choice rather than one model with optional halves: half an
    identity can neither replay nor conflict, and a generator should refuse it
    before a server has to.
    """

    model_config = ConfigDict(extra="forbid")
    label: str = Field(
        min_length=1,
        max_length=200,
        pattern=NON_BLANK,
        description=(
            "Non-blank, up to 200 characters — the length Android's local model "
            "already accepts. Whitespace alone is not a label. The 0.4 server-minted "
            "form keeps its 120-character limit."
        ),
    )
    client_id: UUID = Field(
        description=(
            "**Your identifier for this block, unique within your tenant.** Minted "
            "before the tag is programmed and always supplied, so a registration "
            "replayed after any offline period cannot duplicate. It is **not** the "
            "canonical id: Service generates that and returns both. Two tenants may "
            "use the same value for unrelated blocks, and neither can learn anything "
            "about the other."
        )
    )
    tag_token: str = Field(
        min_length=43,
        max_length=43,
        description=(
            "The canonical ADR-0014 token: 32 random bytes as exactly 43 characters "
            "of unpadded base64url. Send the token only — never the whole "
            "`blocks://tag/v1/<token>` URI. A padded or standard-alphabet spelling of "
            "the same bytes is refused, because two spellings would hash differently "
            "and one physical tag would become two blocks. Service stores only the "
            "hash and **never returns this value**: you already hold it."
        ),
    )


BlockIn = LegacyBlockIn | ClientBlockIn


class BlockOut(BaseModel):
    id: UUID
    label: str
    state: str
    bound_project_id: UUID | None = None


class RegisteredBlockOut(BlockOut):
    """Result of the API 0.4 server-minted form. Unchanged from 0.4."""

    tag_token: str = Field(
        description=(
            "The token to write to the NFC tag. Returned ONCE, here, because this "
            "is the only place a caller of the server-minted form can learn it; the "
            "service stores only its hash and cannot show it again."
        )
    )


class ClientRegisteredBlockOut(BlockOut):
    """Result of the API 0.5 client-supplied form.

    **No `tag_token`, by construction.** You supplied it, so returning it would add
    nothing — and it would not merely be displayed: idempotency persists a
    response as JSON, so echoing the token would write the raw value into Service
    storage, which is exactly what hashing it was for. `client_id` and the
    canonical `id` are what a client needs to attribute the acknowledgement and
    rewrite its dependent binding.
    """

    client_id: UUID


class ReplayedBlockOut(BlockOut):
    """A registration recognised by resource identity after the result expired.

    Current state, not the original bytes — and no `tag_token` for either form: a
    resource replay is a read, and the raw token was never stored to re-read.
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
    response_model=RegisteredBlockOut | ClientRegisteredBlockOut,
    responses={
        **errors.error_responses(400, 401, 403, 409, 426),
        **replay_responses(ReplayedBlockOut),
    },
    summary="Register a physical block",
)
async def register(
    body: BlockIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    client_id = getattr(body, "client_id", None)

    async def produce() -> dict:
        try:
            block, minted, replayed = await loop.register_block(
                ctx.conn,
                ctx.tenant_id,
                body.label,
                client_id=client_id,
                token=getattr(body, "tag_token", None),
            )
        except loop.DomainError as exc:
            raise as_http(exc) from exc
        if replayed:
            raise _ResourceReplay(
                ReplayedBlockOut(
                    id=block.id, label=block.label, state=block.state, client_id=client_id
                ).model_dump(mode="json")
            )
        if client_id is not None:
            return ClientRegisteredBlockOut(
                id=block.id, label=block.label, state=block.state, client_id=client_id
            ).model_dump(mode="json")
        assert minted is not None
        return RegisteredBlockOut(
            id=block.id, label=block.label, state=block.state, tag_token=minted
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
