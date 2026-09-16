"""Discrete project items: Notes, resource Links, and navigation aliases.

API 0.6 (ADR-0021, ADR-0022), built from Android's published project-items sync
boundary rather than a guess at it.

Three rules shape everything here, and each is enforced where it is hard to
bypass rather than remembered at each call site:

  identity is the client's UUID, scoped by tenant -- a composite primary key, so
  there is no global index for a create to probe;

  deletion is a tombstone -- `blocks_app` holds no DELETE on these tables, so
  history cannot be removed by a bug in this file;

  authored text never leaves the row -- events carry identifiers only, which is
  checked by a test over the published event schemas, not by reviewer attention.

The API 0.4 singleton `project_notes` is not read, written or derived from
anywhere in this module. ADR-0022 made that a clean break, so the two surfaces
coexist without either projecting into the other.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit
from uuid import UUID

import psycopg

from . import events
from .operating_loop import ConcurrencyConflict, DomainError, NotFound

#: Android trims, rejects empty/control-bearing values, and accepts only these
#: absolute schemes. Service enforces the same storage allowlist and never
#: resolves, fetches or launches the value -- a stored URI is data, not an action.
ALLOWED_SCHEMES = ("http", "https", "obsidian")

NOTE_BODY_MAX = 100_000
LINK_TITLE_MAX = 200

NOTE_COLUMNS = (
    "id, project_id, body, pinned, authored_by_user_id, created_at, updated_at,"
    " row_version, deleted_at, last_operation_id"
)
LINK_COLUMNS = (
    "id, project_id, title, uri, created_at, updated_at,"
    " row_version, deleted_at, last_operation_id"
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ItemIdentityConflict(ConcurrencyConflict):
    """This client item id is already used here by a different creation."""


class VersionConflict(ConcurrencyConflict):
    """The caller's If-Match row_version is stale."""


class InvalidUri(DomainError):
    """The link URI is not an accepted absolute form."""


def validate_uri(raw: str) -> str:
    """Accept a logical URI, or refuse it. Never resolve it.

    Validation mirrors what Android already enforces before its local write, so
    the two agree on what is storable. Refusing `intent:`, `file:` and `content:`
    matters even though Service never launches anything: a value stored here is
    one a *client* may later hand to a platform handler, and the allowlist is the
    only place that decision is made once for every client.
    """
    uri = raw.strip()
    if not uri or _CONTROL.search(uri):
        raise InvalidUri("uri must be a non-empty value without control characters")
    parts = urlsplit(uri)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise InvalidUri(
            "uri scheme must be one of " + ", ".join(ALLOWED_SCHEMES)
        )
    if parts.username or parts.password:
        raise InvalidUri("uri must not carry user-info credentials")
    if parts.scheme in ("http", "https") and not parts.hostname:
        raise InvalidUri("http and https uris require a host")
    if parts.scheme == "obsidian" and not (parts.netloc or parts.path):
        raise InvalidUri("obsidian uris require a destination")
    return uri


def note_fingerprint(tenant_id: UUID, item_id: UUID, body: str) -> bytes:
    """Durable creation identity: tenant, client id and the INITIAL body."""
    return _fingerprint(tenant_id, item_id, body)


def link_fingerprint(tenant_id: UUID, item_id: UUID, title: str, uri: str) -> bytes:
    return _fingerprint(tenant_id, item_id, title, uri)


def _fingerprint(tenant_id: UUID, item_id: UUID, *parts: str) -> bytes:
    h = hashlib.sha256()
    h.update(str(tenant_id).encode())
    for part in (str(item_id), *parts):
        h.update(b"\x00")
        h.update(part.encode())
    return h.digest()


async def _project_visible(conn: psycopg.AsyncConnection, project_id: UUID) -> bool:
    """Prove the parent is visible in THIS tenant before writing anything.

    Absent and foreign are the same answer by construction: the query runs under
    the tenant policy, so a project in another tenant simply is not there.
    """
    cur = await conn.execute(
        "select 1 from blocks.projects where id = %s", (project_id,)
    )
    return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


async def create_note(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    *,
    item_id: UUID,
    body: str,
    author_id: UUID,
    operation_id: UUID,
) -> tuple[dict, bool]:
    """Create a note item. Returns (item, replayed).

    The author comes from the authenticated principal, never the request. A
    client-supplied author would be an assertion Service cannot check, and
    Android's `local` sentinel is private projection state that maps to no wire
    value at all.
    """
    if len(body) > NOTE_BODY_MAX:
        raise InvalidUri(f"body must be at most {NOTE_BODY_MAX} characters")
    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")

    fingerprint = note_fingerprint(tenant_id, item_id, body)
    existing = await _read_note(conn, item_id)
    if existing is not None:
        return _replay_or_conflict(existing, fingerprint), True

    try:
        async with conn.transaction():      # savepoint: a lost race is not fatal
            cur = await conn.execute(
                "insert into blocks.project_note_items"
                " (tenant_id, id, project_id, body, authored_by_user_id,"
                "  creation_fingerprint, last_operation_id)"
                f" values (%s, %s, %s, %s, %s, %s, %s) returning {NOTE_COLUMNS}",
                (tenant_id, item_id, project_id, body, author_id,
                 fingerprint, operation_id),
            )
    except psycopg.errors.UniqueViolation:
        winner = await _read_note(conn, item_id)
        if winner is None:
            raise
        return _replay_or_conflict(winner, fingerprint), True

    row = await cur.fetchone()
    assert row is not None
    await _emit(conn, tenant_id, "project.note_created", row, operation_id)
    return row, False


async def edit_note(
    conn: psycopg.AsyncConnection, tenant_id: UUID, item_id: UUID,
    *, body: str, if_match: int, operation_id: UUID,
) -> dict:
    if len(body) > NOTE_BODY_MAX:
        raise InvalidUri(f"body must be at most {NOTE_BODY_MAX} characters")
    row = await _update_note(
        conn, item_id, if_match, operation_id,
        "body = %s", (body,),
    )
    await _emit(conn, tenant_id, "project.note_edited", row, operation_id)
    return row


async def pin_note(
    conn: psycopg.AsyncConnection, tenant_id: UUID, item_id: UUID,
    *, pinned: bool, if_match: int, operation_id: UUID,
) -> dict:
    row = await _update_note(
        conn, item_id, if_match, operation_id, "pinned = %s", (pinned,)
    )
    await _emit(conn, tenant_id, "project.note_pinned", row, operation_id)
    return row


async def delete_note(
    conn: psycopg.AsyncConnection, tenant_id: UUID, item_id: UUID,
    *, if_match: int, operation_id: UUID,
) -> dict:
    """Tombstone, not removal. `blocks_app` holds no DELETE on the table."""
    row = await _update_note(
        conn, item_id, if_match, operation_id,
        "deleted_at = coalesce(deleted_at, now())", (),
    )
    await _emit(conn, tenant_id, "project.note_deleted", row, operation_id)
    return row


async def _update_note(
    conn: psycopg.AsyncConnection, item_id: UUID, if_match: int,
    operation_id: UUID, assignment: str, params: tuple,
) -> dict:
    cur = await conn.execute(
        f"update blocks.project_note_items set {assignment},"
        " row_version = row_version + 1, updated_at = now(),"
        " last_operation_id = %s"
        " where id = %s and row_version = %s"
        f" returning {NOTE_COLUMNS}",
        (*params, operation_id, item_id, if_match),
    )
    row = await cur.fetchone()
    if row is None:
        await _explain_update_miss(conn, "project_note_items", item_id)
    return row


async def _read_note(conn: psycopg.AsyncConnection, item_id: UUID) -> dict | None:
    cur = await conn.execute(
        f"select {NOTE_COLUMNS}, creation_fingerprint from blocks.project_note_items"
        " where id = %s",
        (item_id,),
    )
    return await cur.fetchone()


async def list_notes(
    conn: psycopg.AsyncConnection, project_id: UUID
) -> list[dict]:
    """Pinned first, then newest -- with `id` last so the order is TOTAL.

    Tombstones are included: a client that pulls must learn about deletions it
    has not seen, and omitting them would make a delete indistinguishable from a
    row that never existed.
    """
    cur = await conn.execute(
        f"select {NOTE_COLUMNS} from blocks.project_note_items"
        " where project_id = %s"
        " order by pinned desc, created_at desc, id desc",
        (project_id,),
    )
    return await cur.fetchall()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


async def create_link(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    *,
    item_id: UUID,
    title: str,
    uri: str,
    operation_id: UUID,
) -> tuple[dict, bool]:
    uri = validate_uri(uri)
    if len(title) > LINK_TITLE_MAX or not title.strip():
        raise InvalidUri(f"title must be non-blank and at most {LINK_TITLE_MAX} characters")
    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")

    fingerprint = link_fingerprint(tenant_id, item_id, title, uri)
    existing = await _read_link(conn, item_id)
    if existing is not None:
        return _replay_or_conflict(existing, fingerprint), True

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "insert into blocks.project_resource_links"
                " (tenant_id, id, project_id, title, uri, creation_fingerprint,"
                "  last_operation_id)"
                f" values (%s, %s, %s, %s, %s, %s, %s) returning {LINK_COLUMNS}",
                (tenant_id, item_id, project_id, title, uri, fingerprint, operation_id),
            )
    except psycopg.errors.UniqueViolation:
        winner = await _read_link(conn, item_id)
        if winner is None:
            raise
        return _replay_or_conflict(winner, fingerprint), True

    row = await cur.fetchone()
    assert row is not None
    await _emit(conn, tenant_id, "project.link_created", row, operation_id)
    return row, False


async def edit_link(
    conn: psycopg.AsyncConnection, tenant_id: UUID, item_id: UUID,
    *, title: str, uri: str, if_match: int, operation_id: UUID,
) -> dict:
    uri = validate_uri(uri)
    if len(title) > LINK_TITLE_MAX or not title.strip():
        raise InvalidUri(f"title must be non-blank and at most {LINK_TITLE_MAX} characters")
    row = await _update_link(
        conn, item_id, if_match, operation_id, "title = %s, uri = %s", (title, uri)
    )
    await _emit(conn, tenant_id, "project.link_edited", row, operation_id)
    return row


async def delete_link(
    conn: psycopg.AsyncConnection, tenant_id: UUID, item_id: UUID,
    *, if_match: int, operation_id: UUID,
) -> dict:
    row = await _update_link(
        conn, item_id, if_match, operation_id,
        "deleted_at = coalesce(deleted_at, now())", (),
    )
    await _emit(conn, tenant_id, "project.link_deleted", row, operation_id)
    return row


async def _update_link(
    conn: psycopg.AsyncConnection, item_id: UUID, if_match: int,
    operation_id: UUID, assignment: str, params: tuple,
) -> dict:
    cur = await conn.execute(
        f"update blocks.project_resource_links set {assignment},"
        " row_version = row_version + 1, updated_at = now(),"
        " last_operation_id = %s"
        " where id = %s and row_version = %s"
        f" returning {LINK_COLUMNS}",
        (*params, operation_id, item_id, if_match),
    )
    row = await cur.fetchone()
    if row is None:
        await _explain_update_miss(conn, "project_resource_links", item_id)
    return row


async def _read_link(conn: psycopg.AsyncConnection, item_id: UUID) -> dict | None:
    cur = await conn.execute(
        f"select {LINK_COLUMNS}, creation_fingerprint from blocks.project_resource_links"
        " where id = %s",
        (item_id,),
    )
    return await cur.fetchone()


async def list_links(conn: psycopg.AsyncConnection, project_id: UUID) -> list[dict]:
    cur = await conn.execute(
        f"select {LINK_COLUMNS} from blocks.project_resource_links"
        " where project_id = %s order by created_at desc, id desc",
        (project_id,),
    )
    return await cur.fetchall()


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


async def _explain_update_miss(
    conn: psycopg.AsyncConnection, table: str, item_id: UUID
) -> None:
    """An update matched nothing. Say WHICH, without leaking across tenants.

    Absent and foreign are one answer -- the row simply is not visible under this
    tenant's policy. A version mismatch is a different answer, and the client
    needs it to know a re-read will help.
    """
    cur = await conn.execute(
        f"select 1 from blocks.{table} where id = %s", (item_id,)
    )
    if await cur.fetchone() is None:
        raise NotFound("no such item in this tenant")
    raise VersionConflict(
        "the item was modified by someone else; re-read and retry with the "
        "current row_version"
    )


def _replay_or_conflict(existing: dict, fingerprint: bytes) -> dict:
    row = dict(existing)
    stored = row.pop("creation_fingerprint", None)
    if stored is None or bytes(stored) != fingerprint:
        raise ItemIdentityConflict(
            "that item id is already used in this tenant by a different creation; "
            "reconcile against the existing item"
        )
    return row


async def _emit(
    conn: psycopg.AsyncConnection, tenant_id: UUID, event_type: str,
    row: dict, operation_id: UUID,
) -> None:
    """Identifier-only. No body, title, URI or any other authored text.

    The payload is built from a fixed set of keys rather than from the row, so a
    column added later cannot quietly start appearing in the change feed.
    """
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type=event_type,
        payload={
            "operationId": str(operation_id),
            "itemId": str(row["id"]),
            "projectId": str(row["project_id"]),
            "rowVersion": row["row_version"],
            "deleted": row["deleted_at"] is not None,
        },
    )


# ---------------------------------------------------------------------------
# Project navigation aliases
# ---------------------------------------------------------------------------


async def attach_alias(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID, project_key: UUID
) -> tuple[dict, bool]:
    """Attach an Android-held navigation key to an authorized project.

    This is a SEPARATE identity from `projects.client_id`, and the separation is
    the finding the accepted project-link review turned on: a key allocated
    before, or independently of, Service identity is not the creation identity and
    must never be rotated into one. Keeping it in its own table also avoids
    `projects_client_identity_complete`, which would otherwise demand a
    `creation_fingerprint` attesting to a create that never happened.

    Idempotent by (tenant, project, key). Returns (alias, replayed).
    """
    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")

    cur = await conn.execute(
        "select project_key, project_id, created_at"
        " from blocks.project_navigation_aliases where project_key = %s",
        (project_key,),
    )
    existing = await cur.fetchone()
    if existing is not None:
        if existing["project_id"] != project_id:
            # The same key naming a different project. One declared rule, and it
            # is never a silent rotation: a key already printed on a tag or QR
            # cannot be quietly repointed.
            raise ItemIdentityConflict(
                "that project key is already attached to a different project in "
                "this tenant; reconcile against the existing association"
            )
        return existing, True

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "insert into blocks.project_navigation_aliases"
                " (tenant_id, project_key, project_id) values (%s, %s, %s)"
                " returning project_key, project_id, created_at",
                (tenant_id, project_key, project_id),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise ItemIdentityConflict(
            "that project key is already attached in this tenant"
        ) from exc
    row = await cur.fetchone()
    assert row is not None
    return row, False


async def resolve_alias(
    conn: psycopg.AsyncConnection, project_key: UUID
) -> UUID:
    """Resolve an opaque key to its project, inside the authorized tenant.

    A key belonging to another tenant returns exactly what an unknown key
    returns. That is not politeness -- distinguishing them is the existence
    oracle the tenant boundary exists to prevent.
    """
    cur = await conn.execute(
        "select project_id from blocks.project_navigation_aliases"
        " where project_key = %s",
        (project_key,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no such project key in this tenant")
    return row["project_id"]


async def aliases_for(conn: psycopg.AsyncConnection, project_id: UUID) -> list[UUID]:
    cur = await conn.execute(
        "select project_key from blocks.project_navigation_aliases"
        " where project_id = %s order by created_at, project_key",
        (project_id,),
    )
    return [r["project_key"] for r in await cur.fetchall()]
