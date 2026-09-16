"""Discrete project items: Notes, resource Links, navigation aliases (API 0.6).

Items live under `/projects/{id}/items/`, NOT `/projects/{id}/notes`, which API
0.4's singleton already owns -- a shared path would let one surface shadow the
other, the coupling ADR-0022 exists to prevent.

Mutations are made durable by OPERATION RECEIPTS rather than the generic
Idempotency-Key mechanism: `operation_id` is the identity an offline client retires
a queued mutation against, so it must be enforced for every create, edit, pin and
delete, bound to the complete envelope, for the life of the item.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import errors, project_items as items
from .deps import TenantContext, as_http, if_match_version, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects", tags=["project-items"])

OPERATION_ID = Field(
    description=(
        "**The durable identity of this mutation.** Service records a receipt bound "
        "to the complete envelope — project, item, mutation type, If-Match and "
        "payload. Resending the exact envelope returns the recorded result with no "
        "second write and no second event; reusing the id with any other envelope "
        "is `409`. Never rotate it on timeout, restart or an ambiguous response."
    )
)


class _Item(BaseModel):
    tenant_id: UUID
    id: UUID = Field(description="Your tenant-scoped item id, echoed.")
    project_id: UUID
    created_at: datetime
    updated_at: datetime
    row_version: int = Field(description="Send back as `If-Match` on the next mutation.")
    deleted_at: datetime | None = Field(
        description="Set once tombstoned. Terminal: the row is retained and accepts no "
                    "further mutation except an exact replay of that delete.")
    operation_id: UUID = Field(
        description="The operation whose acknowledgement produced this state.")


class NoteOut(_Item):
    body: str
    pinned: bool
    authored_by_user_id: UUID | None = Field(
        description="Derived from the authenticated principal; never client-supplied.")


class LinkOut(_Item):
    title: str
    uri: str = Field(description="The accepted logical value, returned unchanged.")


NON_BLANK = r"\S"


class NoteCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(description="Your tenant-scoped item id. It IS the item's identity.")
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


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID = OPERATION_ID


class LinkCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID = Field(description="Your tenant-scoped item id. It IS the item's identity.")
    title: str = Field(min_length=1, max_length=items.LINK_TITLE_MAX, pattern=NON_BLANK)
    uri: str = Field(
        description=(
            "An absolute `http`, `https` or `obsidian` URI. The scheme is compared "
            "case-insensitively and preserved as written. `http`/`https` require a "
            "well-formed authority with a valid host and no user-info; `obsidian` "
            "requires a destination. Surrounding whitespace is trimmed. Service never "
            "resolves, fetches or launches the value."
        )
    )
    operation_id: UUID = OPERATION_ID


class LinkEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=items.LINK_TITLE_MAX, pattern=NON_BLANK)
    uri: str
    operation_id: UUID = OPERATION_ID


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: UUID = Field(
        description=(
            "**The navigation key you already hold.** A distinct identity from "
            "`client_id`. One key per project and one project per key within your "
            "tenant; an attached key is never rotated or repointed."
        )
    )


class AliasOut(BaseModel):
    project_key: UUID
    project_id: UUID
    created_at: datetime


class ResolveKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: UUID


class ResolveKeyOut(BaseModel):
    project_id: UUID


def _out(model: type[_Item], row: dict) -> dict:
    return model(**{**row, "operation_id": row["last_operation_id"]}).model_dump(mode="json")


def _respond(model, row: dict, replayed: bool, created_status: int) -> JSONResponse:
    headers = {"Idempotent-Replay": "true"} if replayed else {}
    return JSONResponse(status_code=created_status, content=_out(model, row), headers=headers)


MUTATION_ERRORS = errors.error_responses(400, 401, 403, 404, 409, 426)
READ_ERRORS = errors.error_responses(401, 403, 404, 426)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@router.post("/{project_id}/items/notes", status_code=status.HTTP_201_CREATED,
             response_model=NoteOut, responses=MUTATION_ERRORS, summary="Create a note item")
async def create_note(
    project_id: UUID, body: NoteCreateIn, ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    if len(body.body) > items.NOTE_BODY_MAX:
        raise errors.invalid_request("body is too long")
    try:
        row, replayed = await items.create_item(
            ctx.conn, ctx.tenant_id, project_id, "note",
            item_id=body.id, operation_id=body.operation_id,
            content=(body.body,), insert_values={"body": body.body},
            author_id=ctx.actor_id,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _respond(NoteOut, row, replayed, status.HTTP_201_CREATED)


@router.get("/{project_id}/items/notes", response_model=list[NoteOut],
            responses=READ_ERRORS, summary="List note items, tombstones included")
async def list_notes(project_id: UUID, ctx: TenantContext = Depends(tenant_context)) -> list:
    try:
        return [_out(NoteOut, r) for r in await items.list_items(ctx.conn, "note", project_id)]
    except items.DomainError as exc:
        raise as_http(exc) from exc


@router.get("/{project_id}/items/notes/{item_id}", response_model=NoteOut,
            responses=READ_ERRORS, summary="Read one note item — the conflict-recovery read")
async def read_note(
    project_id: UUID, item_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> dict:
    """The authoritative read to take after a `409`, before reconciling."""
    try:
        return _out(NoteOut, await items.read_item(ctx.conn, "note", project_id, item_id))
    except items.DomainError as exc:
        raise as_http(exc) from exc


async def _note_mutation(ctx, project_id, item_id, version, operation_id, mutation,
                         payload, assignment, params, event_type) -> JSONResponse:
    try:
        row, replayed = await items.mutate_item(
            ctx.conn, ctx.tenant_id, project_id, "note", item_id,
            mutation=mutation, if_match=version, operation_id=operation_id,
            payload=payload, assignment=assignment, params=params, event_type=event_type,
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _respond(NoteOut, row, replayed, status.HTTP_200_OK)


@router.patch("/{project_id}/items/notes/{item_id}", response_model=NoteOut,
              responses=MUTATION_ERRORS, summary="Edit a note body")
async def edit_note(project_id: UUID, item_id: UUID, body: NoteEditIn,
                    ctx: TenantContext = Depends(tenant_context),
                    version: int = Depends(if_match_version)) -> JSONResponse:
    return await _note_mutation(ctx, project_id, item_id, version, body.operation_id,
                                "edit", (body.body,), "body = %s", (body.body,),
                                "project.note_edited")


@router.put("/{project_id}/items/notes/{item_id}/pin", response_model=NoteOut,
            responses=MUTATION_ERRORS, summary="Pin or unpin a note")
async def pin_note(project_id: UUID, item_id: UUID, body: NotePinIn,
                   ctx: TenantContext = Depends(tenant_context),
                   version: int = Depends(if_match_version)) -> JSONResponse:
    return await _note_mutation(ctx, project_id, item_id, version, body.operation_id,
                                "pin", (body.pinned,), "pinned = %s", (body.pinned,),
                                "project.note_pinned")


@router.post("/{project_id}/items/notes/{item_id}/delete", response_model=NoteOut,
             responses=MUTATION_ERRORS, summary="Tombstone a note")
async def delete_note(project_id: UUID, item_id: UUID, body: DeleteIn,
                      ctx: TenantContext = Depends(tenant_context),
                      version: int = Depends(if_match_version)) -> JSONResponse:
    return await _note_mutation(ctx, project_id, item_id, version, body.operation_id,
                                "delete", (), "deleted_at = now()", (),
                                "project.note_deleted")


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@router.post("/{project_id}/items/links", status_code=status.HTTP_201_CREATED,
             response_model=LinkOut, responses=MUTATION_ERRORS, summary="Create a resource link")
async def create_link(
    project_id: UUID, body: LinkCreateIn, ctx: TenantContext = Depends(tenant_context),
) -> JSONResponse:
    try:
        uri = items.validate_uri(body.uri)
        row, replayed = await items.create_item(
            ctx.conn, ctx.tenant_id, project_id, "link",
            item_id=body.id, operation_id=body.operation_id,
            content=(body.title, uri), insert_values={"title": body.title, "uri": uri},
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _respond(LinkOut, row, replayed, status.HTTP_201_CREATED)


@router.get("/{project_id}/items/links", response_model=list[LinkOut],
            responses=READ_ERRORS, summary="List resource links, tombstones included")
async def list_links(project_id: UUID, ctx: TenantContext = Depends(tenant_context)) -> list:
    try:
        return [_out(LinkOut, r) for r in await items.list_items(ctx.conn, "link", project_id)]
    except items.DomainError as exc:
        raise as_http(exc) from exc


@router.get("/{project_id}/items/links/{item_id}", response_model=LinkOut,
            responses=READ_ERRORS, summary="Read one resource link — the conflict-recovery read")
async def read_link(
    project_id: UUID, item_id: UUID, ctx: TenantContext = Depends(tenant_context)
) -> dict:
    try:
        return _out(LinkOut, await items.read_item(ctx.conn, "link", project_id, item_id))
    except items.DomainError as exc:
        raise as_http(exc) from exc


@router.patch("/{project_id}/items/links/{item_id}", response_model=LinkOut,
              responses=MUTATION_ERRORS, summary="Edit a link title and URI")
async def edit_link(project_id: UUID, item_id: UUID, body: LinkEditIn,
                    ctx: TenantContext = Depends(tenant_context),
                    version: int = Depends(if_match_version)) -> JSONResponse:
    try:
        uri = items.validate_uri(body.uri)
        row, replayed = await items.mutate_item(
            ctx.conn, ctx.tenant_id, project_id, "link", item_id,
            mutation="edit", if_match=version, operation_id=body.operation_id,
            payload=(body.title, uri), assignment="title = %s, uri = %s",
            params=(body.title, uri), event_type="project.link_edited",
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _respond(LinkOut, row, replayed, status.HTTP_200_OK)


@router.post("/{project_id}/items/links/{item_id}/delete", response_model=LinkOut,
             responses=MUTATION_ERRORS, summary="Tombstone a resource link")
async def delete_link(project_id: UUID, item_id: UUID, body: DeleteIn,
                      ctx: TenantContext = Depends(tenant_context),
                      version: int = Depends(if_match_version)) -> JSONResponse:
    try:
        row, replayed = await items.mutate_item(
            ctx.conn, ctx.tenant_id, project_id, "link", item_id,
            mutation="delete", if_match=version, operation_id=body.operation_id,
            payload=(), assignment="deleted_at = now()", params=(),
            event_type="project.link_deleted",
        )
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return _respond(LinkOut, row, replayed, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Navigation aliases
# ---------------------------------------------------------------------------


@router.post("/{project_id}/navigation-aliases", status_code=status.HTTP_201_CREATED,
             response_model=AliasOut, responses=MUTATION_ERRORS,
             summary="Attach the project's navigation key")
async def attach_alias(project_id: UUID, body: AliasIn,
                       ctx: TenantContext = Depends(tenant_context)) -> JSONResponse:
    try:
        row, replayed = await items.attach_alias(
            ctx.conn, ctx.tenant_id, project_id, body.project_key)
    except items.DomainError as exc:
        raise as_http(exc) from exc
    return JSONResponse(
        status_code=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
        content=AliasOut(**row).model_dump(mode="json"))


@router.post("/resolve-key", response_model=ResolveKeyOut,
             responses=errors.error_responses(400, 401, 403, 404, 426),
             summary="Resolve a navigation key to its project")
async def resolve_key(body: ResolveKeyIn, ctx: TenantContext = Depends(tenant_context)) -> dict:
    """Authenticated POST body, never a URL: a key in a URL is a key in an access log."""
    try:
        return ResolveKeyOut(project_id=await items.resolve_alias(ctx.conn, body.project_key)
                             ).model_dump(mode="json")
    except items.DomainError as exc:
        raise as_http(exc) from exc
