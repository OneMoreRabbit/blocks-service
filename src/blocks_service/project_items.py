"""Discrete project items: Notes, resource Links, and navigation aliases.

API 0.6 (ADR-0021, ADR-0022), built from Android's published project-items sync
boundary and repaired against `meta/blocks-service-api-0-6-review-v0_1.md`.

Four rules shape everything here, each enforced where it is hard to bypass:

  an OPERATION is an identity, not an echo -- every mutation records a durable
  receipt bound to its complete envelope, so a retry replays and a reuse conflicts;

  every item is addressed THROUGH its project -- an item from project B cannot be
  reached, read or mutated through project A's URL;

  a tombstone is terminal -- after deletion only an exact replay of that delete
  succeeds;

  authored text never leaves the row -- events carry identifiers only.

The API 0.4 singleton `project_notes` is not read, written or derived from
anywhere in this module (ADR-0022).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID

import psycopg

from . import events
from .operating_loop import ConcurrencyConflict, DomainError, NotFound

ALLOWED_SCHEMES = ("http", "https", "obsidian")
NOTE_BODY_MAX = 100_000
LINK_TITLE_MAX = 200

NOTE_COLUMNS = (
    "tenant_id, id, project_id, body, pinned, authored_by_user_id, created_at,"
    " updated_at, row_version, deleted_at, last_operation_id"
)
LINK_COLUMNS = (
    "tenant_id, id, project_id, title, uri, created_at, updated_at,"
    " row_version, deleted_at, last_operation_id"
)

_CONTROL = re.compile(r"[\x00-\x20\x7f]")      # control chars AND spaces
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ItemConflict(ConcurrencyConflict):
    """An identity is already used here by a different operation or creation."""


class VersionConflict(ConcurrencyConflict):
    """The caller's If-Match row_version is stale."""


class TombstoneConflict(ConcurrencyConflict):
    """The item is deleted; only an exact replay of that delete may succeed."""


class InvalidItem(DomainError):
    """The request is not an acceptable item value."""


# ---------------------------------------------------------------------------
# URI validation -- ONE canonical rule, shared by handler and database
# ---------------------------------------------------------------------------


def validate_uri(raw: str) -> str:
    """Accept a logical link URI, or refuse it. Never resolve, fetch or launch it.

    The rule, stated once so the handler and the database constraint can agree on
    it (the review found they did not):

    * surrounding whitespace is trimmed; the trimmed value is what is stored and
      returned;
    * no control characters and no interior whitespace;
    * the scheme is `http`, `https` or `obsidian`, compared CASE-INSENSITIVELY
      (RFC 3986 section 3.1), and preserved as written. The database check uses
      `~*` for the same reason, so a value accepted here can never be rejected on
      insert;
    * `http`/`https` need a well-formed authority: a host that is a valid DNS
      name or IP literal, a numeric port in range if present, and no user-info;
    * `obsidian` needs a non-empty destination.

    Every accepted value therefore satisfies the database constraint, which is the
    invariant a shared corpus test checks directly.
    """
    uri = raw.strip()
    if not uri:
        raise InvalidItem("uri must not be empty")
    if _CONTROL.search(uri):
        raise InvalidItem("uri must not contain control characters or whitespace")
    try:
        parts = urlsplit(uri)
    except ValueError as exc:
        raise InvalidItem("uri is malformed") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidItem("uri scheme must be one of " + ", ".join(ALLOWED_SCHEMES))

    if scheme in ("http", "https"):
        if not uri[len(parts.scheme):].startswith("://"):
            raise InvalidItem("http and https uris must have an authority")
        if "@" in parts.netloc:
            raise InvalidItem("uri must not carry user-info credentials")
        try:
            host = parts.hostname
            port = parts.port                       # raises on a malformed port
        except ValueError as exc:
            raise InvalidItem("uri authority is malformed") from exc
        if not host or not _valid_host(host, parts.netloc):
            raise InvalidItem("http and https uris require a valid host")
        if port is not None and not (0 < port <= 65535):
            raise InvalidItem("uri port is out of range")
    else:  # obsidian
        if not (parts.netloc or parts.path.strip("/") or parts.query):
            raise InvalidItem("obsidian uris require a destination")

    return uri


def _valid_host(host: str, netloc: str) -> bool:
    if "[" in netloc or "]" in netloc:
        # An IPv6 literal must be bracketed exactly once and parse.
        if netloc.count("[") != 1 or netloc.count("]") != 1:
            return False
        try:
            ipaddress.IPv6Address(host)
            return True
        except ValueError:
            return False
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    return bool(labels) and all(_HOST_LABEL.match(label) for label in labels)


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def _hash(*parts: object) -> bytes:
    h = hashlib.sha256()
    for part in parts:
        h.update(b"\x00")
        h.update(("" if part is None else str(part)).encode())
    return h.digest()


def envelope_fingerprint(
    project_id: UUID, kind: str, item_id: UUID, mutation: str,
    if_match: int | None, *payload: object,
) -> bytes:
    """The complete immutable envelope an operation id is bound to.

    Project, item kind, item, mutation type, If-Match and payload. A retry carries
    exactly these; any difference is a different operation wearing a reused id.
    """
    return _hash("envelope", project_id, kind, item_id, mutation, if_match, *payload)


def creation_fingerprint(
    tenant_id: UUID, project_id: UUID, kind: str, item_id: UUID,
    operation_id: UUID, *content: object,
) -> bytes:
    """Durable creation identity, now bound to project AND operation.

    The first draft omitted both, so the same item id and body under a different
    project -- or under a different operation id -- replayed as though it were the
    original create, echoing an operation that never happened.
    """
    return _hash("create", tenant_id, project_id, kind, item_id, operation_id, *content)


# ---------------------------------------------------------------------------
# Shared machinery
# ---------------------------------------------------------------------------


async def _project_visible(conn: psycopg.AsyncConnection, project_id: UUID) -> bool:
    cur = await conn.execute("select 1 from blocks.projects where id = %s", (project_id,))
    return await cur.fetchone() is not None


async def _receipt(conn: psycopg.AsyncConnection, operation_id: UUID) -> dict | None:
    cur = await conn.execute(
        "select project_id, item_kind, item_id, mutation, envelope_fingerprint"
        " from blocks.project_item_operations where operation_id = %s",
        (operation_id,),
    )
    return await cur.fetchone()


def _table(kind: str) -> tuple[str, str]:
    return (("blocks.project_note_items", NOTE_COLUMNS) if kind == "note"
            else ("blocks.project_resource_links", LINK_COLUMNS))


async def read_item(
    conn: psycopg.AsyncConnection, kind: str, project_id: UUID, item_id: UUID
) -> dict:
    """Authoritative read, scoped to the project in the path.

    Absent, foreign-tenant and wrong-project are one answer: the query cannot see
    a row in another tenant (policy) or another project (predicate), so all three
    return nothing and all three raise the same NotFound.
    """
    table, columns = _table(kind)
    cur = await conn.execute(
        f"select {columns} from {table} where id = %s and project_id = %s",
        (item_id, project_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no such item in this project")
    return row


async def _replay(
    conn: psycopg.AsyncConnection, receipt: dict, fingerprint: bytes, kind: str,
    project_id: UUID, item_id: UUID,
) -> dict:
    """An operation id seen before: replay it exactly, or refuse the reuse."""
    if bytes(receipt["envelope_fingerprint"]) != fingerprint:
        raise ItemConflict(
            "that operation id was already used for a different mutation; "
            "a retry must resend the exact original envelope"
        )
    return await read_item(conn, kind, project_id, item_id)


async def _record(
    conn: psycopg.AsyncConnection, tenant_id: UUID, operation_id: UUID,
    project_id: UUID, kind: str, item_id: UUID, mutation: str,
    fingerprint: bytes, row_version: int,
) -> None:
    await conn.execute(
        "insert into blocks.project_item_operations"
        " (tenant_id, operation_id, project_id, item_kind, item_id, mutation,"
        "  envelope_fingerprint, accepted_row_version)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, operation_id, project_id, kind, item_id, mutation,
         fingerprint, row_version),
    )


async def _emit(
    conn: psycopg.AsyncConnection, tenant_id: UUID, event_type: str,
    row: dict, operation_id: UUID,
) -> None:
    """Identifier-only, from a fixed key set. No authored text, ever."""
    await events.emit(
        conn, tenant_id=tenant_id, event_type=event_type,
        payload={
            "operationId": str(operation_id),
            "itemId": str(row["id"]),
            "projectId": str(row["project_id"]),
            "rowVersion": row["row_version"],
            "deleted": row["deleted_at"] is not None,
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_item(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID, kind: str,
    *, item_id: UUID, operation_id: UUID, content: tuple, insert_values: dict,
    author_id: UUID | None = None,
) -> tuple[dict, bool]:
    """Create a note or link. Returns (item, replayed)."""
    envelope = envelope_fingerprint(project_id, kind, item_id, "create", None, *content)

    receipt = await _receipt(conn, operation_id)
    if receipt is not None:
        return await _replay(conn, receipt, envelope, kind, project_id, item_id), True

    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")

    table, columns = _table(kind)
    created_fp = creation_fingerprint(
        tenant_id, project_id, kind, item_id, operation_id, *content)
    fields = {"tenant_id": tenant_id, "id": item_id, "project_id": project_id,
              "creation_fingerprint": created_fp, "last_operation_id": operation_id,
              **insert_values}
    if kind == "note":
        fields["authored_by_user_id"] = author_id
    names = ", ".join(fields)
    placeholders = ", ".join(["%s"] * len(fields))

    try:
        async with conn.transaction():          # item and receipt commit together
            cur = await conn.execute(
                f"insert into {table} ({names}) values ({placeholders})"
                f" returning {columns}",
                tuple(fields.values()),
            )
            row = await cur.fetchone()
            await _record(conn, tenant_id, operation_id, project_id, kind, item_id,
                          "create", envelope, row["row_version"])
    except psycopg.errors.UniqueViolation:
        # Either the item id is taken or a concurrent identical request won the
        # receipt. Only the receipt can say which, and only an exact envelope
        # match is a replay.
        receipt = await _receipt(conn, operation_id)
        if receipt is not None:
            return await _replay(conn, receipt, envelope, kind, project_id, item_id), True
        raise ItemConflict(
            "that item id is already used in this tenant by a different operation; "
            "reconcile against the existing item"
        ) from None

    await _emit(conn, tenant_id, f"project.{kind}_created", row, operation_id)
    return row, False


# ---------------------------------------------------------------------------
# Edit, pin, delete
# ---------------------------------------------------------------------------


async def mutate_item(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID, kind: str,
    item_id: UUID, *, mutation: str, if_match: int, operation_id: UUID,
    payload: tuple, assignment: str, params: tuple, event_type: str,
) -> tuple[dict, bool]:
    """Apply one version-checked mutation. Returns (item, replayed).

    Order matters and each step closes a finding from the review:

    1. the RECEIPT first -- an exact retry replays the recorded result with no
       second write and no second event, and a reused id with any other envelope
       conflicts;
    2. the item is read THROUGH its project, so a wrong-project URL is NotFound;
    3. a tombstoned item refuses every mutation (only step 1 lets a delete replay);
    4. the version check;
    5. write and receipt commit together, and a version miss re-checks the
       receipt, because a concurrent identical request may have just won.
    """
    envelope = envelope_fingerprint(project_id, kind, item_id, mutation, if_match, *payload)

    receipt = await _receipt(conn, operation_id)
    if receipt is not None:
        return await _replay(conn, receipt, envelope, kind, project_id, item_id), True

    current = await read_item(conn, kind, project_id, item_id)
    if current["deleted_at"] is not None:
        raise TombstoneConflict(
            "the item is deleted; a deleted item accepts no further mutation"
        )

    table, columns = _table(kind)
    try:
        async with conn.transaction():
            cur = await conn.execute(
                f"update {table} set {assignment},"
                " row_version = row_version + 1, updated_at = now(),"
                " last_operation_id = %s"
                " where id = %s and project_id = %s and row_version = %s"
                " and deleted_at is null"
                f" returning {columns}",
                (*params, operation_id, item_id, project_id, if_match),
            )
            row = await cur.fetchone()
            if row is None:
                raise _StaleOrRaced()
            await _record(conn, tenant_id, operation_id, project_id, kind, item_id,
                          mutation, envelope, row["row_version"])
    except (_StaleOrRaced, psycopg.errors.UniqueViolation):
        receipt = await _receipt(conn, operation_id)
        if receipt is not None:
            return await _replay(conn, receipt, envelope, kind, project_id, item_id), True
        latest = await read_item(conn, kind, project_id, item_id)
        if latest["deleted_at"] is not None:
            raise TombstoneConflict(
                "the item is deleted; a deleted item accepts no further mutation"
            ) from None
        raise VersionConflict(
            "the item was modified by someone else; re-read it and retry with the "
            "current row_version"
        ) from None

    await _emit(conn, tenant_id, event_type, row, operation_id)
    return row, False


class _StaleOrRaced(Exception):
    """Internal: the guarded update matched nothing."""


async def list_items(
    conn: psycopg.AsyncConnection, kind: str, project_id: UUID
) -> list[dict]:
    """Every item in the project, tombstones included, in a TOTAL order.

    The parent is verified first: listing a project that does not exist in this
    tenant is NotFound, not an empty list that would read as "no items".
    """
    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")
    table, columns = _table(kind)
    order = ("pinned desc, created_at desc, id desc" if kind == "note"
             else "created_at desc, id desc")
    cur = await conn.execute(
        f"select {columns} from {table} where project_id = %s order by {order}",
        (project_id,),
    )
    return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# Project navigation aliases
# ---------------------------------------------------------------------------


async def attach_alias(
    conn: psycopg.AsyncConnection, tenant_id: UUID, project_id: UUID, project_key: UUID
) -> tuple[dict, bool]:
    """Attach a navigation key. One key per project; one project per key.

    A DISTINCT identity from `projects.client_id` -- attaching a key fabricates no
    creation evidence. Idempotent by (tenant, project, key), including under a
    concurrent identical attach, which re-reads and succeeds rather than surfacing
    the losing insert as a generic conflict.
    """
    if not await _project_visible(conn, project_id):
        raise NotFound("no such project in this tenant")

    existing = await _alias_state(conn, project_id, project_key)
    if existing is not None:
        return existing, True

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "insert into blocks.project_navigation_aliases"
                " (tenant_id, project_key, project_id) values (%s, %s, %s)"
                " returning project_key, project_id, created_at",
                (tenant_id, project_key, project_id),
            )
            row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        existing = await _alias_state(conn, project_id, project_key)
        if existing is not None:
            return existing, True
        raise  # unreachable: _alias_state raises on any non-identical state
    return row, False


async def _alias_state(
    conn: psycopg.AsyncConnection, project_id: UUID, project_key: UUID
) -> dict | None:
    """None if free; the row if identical; ItemConflict if anything else."""
    cur = await conn.execute(
        "select project_key, project_id, created_at from blocks.project_navigation_aliases"
        " where project_key = %s or project_id = %s",
        (project_key, project_id),
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    for row in rows:
        if row["project_key"] == project_key and row["project_id"] == project_id:
            return row
    if any(r["project_key"] == project_key for r in rows):
        raise ItemConflict(
            "that navigation key is already attached to a different project in "
            "this tenant; a printed key is never repointed"
        )
    raise ItemConflict(
        "this project already has a different navigation key; a project carries "
        "one key, and an attached key is never rotated"
    )


async def resolve_alias(conn: psycopg.AsyncConnection, project_key: UUID) -> UUID:
    """A key from another tenant returns exactly what an unknown key returns."""
    cur = await conn.execute(
        "select project_id from blocks.project_navigation_aliases where project_key = %s",
        (project_key,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no such project key in this tenant")
    return row["project_id"]
