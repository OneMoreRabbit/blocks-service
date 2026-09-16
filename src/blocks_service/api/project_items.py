"""Discrete project items: Notes, resource Links, navigation aliases (API 0.6)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import errors, project_items as items
from ..idempotency import Idempotency
from .deps import (
    TenantContext,
    as_http,
    idempotency,
    if_match_version,
    tenant_context,
)

#: Items live under `/items/`, NOT under `/notes`. API 0.4's singleton notes route
#: already owns `/projects/{id}/notes`, and sharing that path would let one surface
#: shadow the other -- the precise coupling ADR-0022 exists to prevent. A separate
#: path space makes the clean break visible in the URL and impossible to route
#: around by accident.
router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects", tags=["project-items"])

OPERATION_ID = Field(
    description=(
        "**Your stable identity for this mutation.** Returned unchanged on the "
        "result, including a replay, so exactly one queued operation can be "
        "retired against it. A retry sends the same value; it is never rotated "
        "because of a timeout, a restart or an ambiguous response."
    )
)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(
        description=(
            "**Your identifier for this note, unique within your tenant.** It is "
            "the item's identity, not a hint: reads and events echo this value. "
            "Two tenants may use the same UUID for unrelated notes and neither "
            "can learn that the other did."
        )
    )
    body: str = Field(max_length=items.NOTE_BODY_MAX)
    operation_id: UUID = OPERATION_ID


class NoteEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(max_length=items.NOTE_BODY_MAX)
    operation_id: UUID = OPERATION_ID


class NotePinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool
    operation_id: UUID = OPERATION_ID


class NoteDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID = OPERATION_ID


class NoteOut(BaseModel):
    id: UUID
    project_id: UUID
    body: str
    pinned: bool
    authored_by_user_id: UUID | None = Field(
        description="Derived from the authenticated principal; never client-supplied."
    )
    created_at: str
    updated_at: str
    row_version: int
    deleted_at: str | None = Field(
        description="Set once the note is tombstoned. The row is retained."
    )
    operation_id: UUID = Field(
        description="The operation whose acknowledgement produced this state."
    )


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    title: str = Field(min_length=1, max_length=items.LINK_TITLE_MAX)
    uri: str = Field(
        description=(
            "An absolute `http`, `https` or `obsidian` URI. Stored as a logical "
            "value and returned unchanged: Service never resolves, fetches or "
            "launches it. Other schemes, and user-info credentials, are refused."
        )
    )
    operation_id: UUID = OPERATION_ID


class LinkEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=items.LINK_TITLE_MAX)
    uri: str
    operation_id: UUID = OPERATION_ID


class LinkDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID = OPERATION_ID


class LinkOut(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    uri: str
    created_at: str
    updated_at: str
    row_version: int
    deleted_at: str | None = None
    operation_id: UUID


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: UUID = Field(
        description=(
            "**An opaque navigation key you already hold.** A DISTINCT identity "
            "from the project's `client_id`: a key allocated before, or "
            "independently of, Service identity stays distinct and is never "
            "rotated into the creation identity or inferred from it."
        )
    )


class AliasOut(BaseModel):
    project_key: UUID
    project_id: UUID
    created_at: str


class ResolveKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: UUID


class ResolveKeyOut(BaseModel):
    project_id: UUID


def _note_out(row: dict, operation_id: UUID) -> dict:
    return NoteOut(
        id=row["id"], project_id=row["project_id"], body=row["body"],
        pinned=row["pinned"], authored_by_user_id=row["authored_by_user_id"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        row_version=row["row_version"],
        deleted_at=row["deleted_at"].isoformat() if row["deleted_at"] else None,
        operation_id=operation_id,
    ).model_dump(mode="json")


def _link_out(row: dict, operation_id: UUID) -> dict:
    return LinkOut(
        id=row["id"], project_id=row["project_id"], title=row["title"],
        uri=row["uri"], created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        row_version=row["row_version"],
        deleted_at=row["deleted_at"].isoformat() if row["deleted_at"] else None,
        operation_id=operation_id,
    ).model_dump(mode="json")


ERRORS = errors.error_responses(400, 401, 403, 404, 409, 426)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@router.post("/{project_id}/items/notes", status_code=status.HTTP_201_CREATED,
             response_model=NoteOut, responses=ERRORS, summary="Create a note item")
async def create_note(
    project_id: UUID, body: NoteIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row, _ = await items.create_note(
                ctx.conn, ctx.tenant_id, project_id, item_id=body.id,
                body=body.body, author_id=ctx.actor_id, operation_id=body.operation_id,
            )
        except items.DomainError as exc:
            raise as_http(exc) from exc
        return _note_out(row, body.operation_id)

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.patch("/{project_id}/items/notes/{item_id}", response_model=NoteOut,
              responses=ERRORS, summary="Edit a note body")
async def edit_note(
    project_id: UUID, item_id: UUID, body: NoteEditIn,
    ctx: TenantContext = Depends(tenant_context),
    version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await items.edit_note(
            ctx.conn, ctx.tenant_id, item_id, body=body.body,
            if_match=version, operation_id=body.operation_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _note_out(row, body.operation_id)


@router.put("/{project_id}/items/notes/{item_id}/pin", response_model=NoteOut,
            responses=ERRORS, summary="Pin or unpin a note")
async def pin_note(
    project_id: UUID, item_id: UUID, body: NotePinIn,
    ctx: TenantContext = Depends(tenant_context),
    version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await items.pin_note(
            ctx.conn, ctx.tenant_id, item_id, pinned=body.pinned,
            if_match=version, operation_id=body.operation_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _note_out(row, body.operation_id)


@router.post("/{project_id}/items/notes/{item_id}/delete", response_model=NoteOut,
             responses=ERRORS, summary="Tombstone a note")
async def delete_note(
    project_id: UUID, item_id: UUID, body: NoteDeleteIn,
    ctx: TenantContext = Depends(tenant_context),
    version: int = Depends(if_match_version),
) -> dict:
    """A tombstone, not a removal: the row is retained and appears in reads."""
    try:
        row = await items.delete_note(
            ctx.conn, ctx.tenant_id, item_id,
            if_match=version, operation_id=body.operation_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _note_out(row, body.operation_id)


@router.get("/{project_id}/items/notes", response_model=list[NoteOut],
            responses=errors.error_responses(401, 403, 404, 426),
            summary="List note items, including tombstones")
async def list_notes(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[dict]:
    rows = await items.list_notes(ctx.conn, project_id)
    return [_note_out(r, r["last_operation_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@router.post("/{project_id}/items/links", status_code=status.HTTP_201_CREATED,
             response_model=LinkOut, responses=ERRORS, summary="Create a resource link")
async def create_link(
    project_id: UUID, body: LinkIn,
    ctx: TenantContext = Depends(tenant_context),
    idem: Idempotency = Depends(idempotency),
) -> JSONResponse:
    async def produce() -> dict:
        try:
            row, _ = await items.create_link(
                ctx.conn, ctx.tenant_id, project_id, item_id=body.id,
                title=body.title, uri=body.uri, operation_id=body.operation_id,
            )
        except items.DomainError as exc:
            raise as_http(exc) from exc
        return _link_out(row, body.operation_id)

    return await idem.run(status.HTTP_201_CREATED, produce)


@router.patch("/{project_id}/items/links/{item_id}", response_model=LinkOut,
              responses=ERRORS, summary="Edit a link title and URI")
async def edit_link(
    project_id: UUID, item_id: UUID, body: LinkEditIn,
    ctx: TenantContext = Depends(tenant_context),
    version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await items.edit_link(
            ctx.conn, ctx.tenant_id, item_id, title=body.title, uri=body.uri,
            if_match=version, operation_id=body.operation_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _link_out(row, body.operation_id)


@router.post("/{project_id}/items/links/{item_id}/delete", response_model=LinkOut,
             responses=ERRORS, summary="Tombstone a resource link")
async def delete_link(
    project_id: UUID, item_id: UUID, body: LinkDeleteIn,
    ctx: TenantContext = Depends(tenant_context),
    version: int = Depends(if_match_version),
) -> dict:
    try:
        row = await items.delete_link(
            ctx.conn, ctx.tenant_id, item_id,
            if_match=version, operation_id=body.operation_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _link_out(row, body.operation_id)


@router.get("/{project_id}/items/links", response_model=list[LinkOut],
            responses=errors.error_responses(401, 403, 404, 426),
            summary="List resource links, including tombstones")
async def list_links(
    project_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> list[dict]:
    rows = await items.list_links(ctx.conn, project_id)
    return [_link_out(r, r["last_operation_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Navigation aliases
# ---------------------------------------------------------------------------


@router.post("/{project_id}/navigation-aliases", status_code=status.HTTP_201_CREATED,
             response_model=AliasOut, responses=ERRORS,
             summary="Attach a navigation key to a project")
async def attach_alias(
    project_id: UUID, body: AliasIn,
    ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    try:
        row, replayed = await items.attach_alias(
            ctx.conn, ctx.tenant_id, project_id, body.project_key
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return JSONResponse(
        status_code=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
        content=AliasOut(
            project_key=row["project_key"], project_id=row["project_id"],
            created_at=row["created_at"].isoformat(),
        ).model_dump(mode="json"),
    )


@router.post("/resolve-key", response_model=ResolveKeyOut,
             responses=errors.error_responses(400, 401, 403, 404, 426),
             summary="Resolve a navigation key to its project")
async def resolve_key(
    body: ResolveKeyIn, ctx: TenantContext = Depends(tenant_context)
) -> dict:
    """An authenticated POST body, never a path or query parameter.

    A key in a URL is a key in an access log, and ADR-0016 requires it to stay out
    of both. A key belonging to another tenant answers exactly as an unknown one.
    """
    try:
        project_id = await items.resolve_alias(ctx.conn, body.project_key)
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return ResolveKeyOut(project_id=project_id).model_dump(mode="json")
