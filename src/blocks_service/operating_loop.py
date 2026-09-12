"""The operating loop: blocks, bindings, the map, work sessions, parking.

The product. Every function here runs on a connection already scoped to a tenant,
so none of them carries a tenant predicate -- see `db.py` for why that is the
design rather than an omission.

Three rules from the constitution are enforced rather than assumed, and each is
enforced in the place that makes it hard to bypass:

  the map is timeless -- there is no date column to write to (migration 0004);
  a block commits to a core outcome -- a NOT NULL, non-blank constraint;
  parking is append-only -- `blocks_app` holds no UPDATE on the table at all.

What lives here instead is the sequencing those rules do not cover: which state
transitions are legal, what a re-entry view is, and how two devices racing the
same session are resolved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from . import events

#: Sparse ordering. Inserting between two items takes the midpoint, so a reorder
#: writes one row instead of renumbering the list -- which for offline clients
#: would turn every local reorder into a conflict with every other edit.
SORT_GAP = 1_000_000

#: The legal moves. Anything absent is rejected before it reaches the database,
#: so an illegal transition is a 409 with a readable reason rather than a
#: constraint violation the client cannot interpret.
TRANSITIONS: dict[str, set[str]] = {
    "active": {"parked", "closed", "cancelled"},
    "parked": {"active", "closed", "cancelled"},
    "closed": set(),
    "cancelled": set(),
}


class DomainError(Exception):
    """A rule of the operating loop was broken."""


class NotFound(DomainError):
    pass


class ConcurrencyConflict(DomainError):
    """The caller's expected row_version is stale, or the row moved underneath it."""


class IllegalTransition(DomainError):
    pass


class ActiveSessionExists(DomainError):
    """One active session per user: park or close the current one first."""


class BlockIdentityConflict(ConcurrencyConflict):
    """A client block identity or tag token is already used differently here.

    Deliberately one error for two causes -- a reused `client_id` under a
    different operation, and a token already assigned to another block in this
    tenant. The caller learns that *this* registration cannot proceed and nothing
    about which of its values was the problem, because saying which would answer a
    question the caller may not be entitled to ask.
    """


# ---------------------------------------------------------------------------
# Physical blocks and bindings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    id: UUID
    label: str
    state: str


def hash_token(token: str) -> bytes:
    """Tags are looked up by hash. The plaintext is never stored.

    A database disclosure therefore does not yield working tags. The token is a
    lookup key and not a credential in either direction -- possession of it
    proves nothing, which is why the pickup flow still requires a bearer token.
    """
    return hashlib.sha256(token.encode()).digest()


#: 32 random bytes as unpadded base64url -- exactly 43 characters (ADR-0014).
#: The tag carries `blocks://tag/v1/<token>`; the token alone reaches this service.
CANONICAL_TOKEN_BYTES = 32
CANONICAL_TOKEN_CHARS = 43


class MalformedToken(DomainError):
    """The supplied token is not the canonical ADR-0014 encoding."""


def canonical_token_bytes(token: str) -> bytes:
    """Decode a client-supplied token, or refuse it.

    Validation is exact rather than lenient. A token that decodes to the right
    length under padded base64, or with standard `+/` alphabet, is *not* the
    canonical form, and accepting it would let two spellings of one secret become
    two different hashes -- so the same physical tag would register twice and
    resolve as two blocks. Re-encoding the decoded bytes and requiring the result
    to equal the input is what makes the encoding canonical rather than merely
    decodable.
    """
    if len(token) != CANONICAL_TOKEN_CHARS:
        raise MalformedToken(
            f"tag_token must be {CANONICAL_TOKEN_CHARS} characters of unpadded base64url"
        )
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (binascii.Error, ValueError) as exc:
        raise MalformedToken("tag_token is not valid unpadded base64url") from exc
    if len(raw) != CANONICAL_TOKEN_BYTES:
        raise MalformedToken(
            f"tag_token must decode to {CANONICAL_TOKEN_BYTES} bytes"
        )
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != token:
        raise MalformedToken("tag_token is not in canonical unpadded base64url form")
    return raw


def block_creation_fingerprint(
    client_id: UUID, label: str, token_hash: bytes
) -> bytes:
    """Durable creation identity for a client-registered block.

    Over the client identity, the label and the token HASH -- never the raw token,
    so the fingerprint is safe to retain beside the row it identifies. Replay
    correctness therefore outlives idempotency-result retention, which ADR-0014
    requires because an offline phone may return long after the result expires.
    """
    h = hashlib.sha256()
    h.update(str(client_id).encode())
    h.update(b"\x00")
    h.update(label.encode())
    h.update(b"\x00")
    h.update(token_hash)
    return h.digest()


def _violated(exc: psycopg.errors.UniqueViolation) -> str:
    """The constraint a unique violation actually names."""
    return getattr(getattr(exc, "diag", None), "constraint_name", "") or ""


BLOCK_COLUMNS = "id, label, state::text as state"


async def register_block(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    label: str,
    *,
    client_id: UUID | None = None,
    token: str | None = None,
) -> tuple[Block, str | None, bool]:
    """Register a block. Returns (block, minted_token, replayed).

    `minted_token` is the plaintext ONLY for the server-minted 0.4 form, where the
    caller has no other way to learn it. For the client-supplied form it is
    `None`: the caller already holds the token, and handing it back would put the
    raw value into the response -- which idempotency then persists as JSON, so an
    echo is not a display decision but a storage one.

    Two shapes, and the caller chooses by supplying both client values or neither:

      neither  -- the API 0.4 flow. The service mints the token and returns it once.
      both     -- the ADR-0014 local-first flow. The phone already programmed the
                  tag offline; the token it wrote is the only one that can work, so
                  the service accepts it rather than issuing a competing value.

    Identity is TENANT-SCOPED, for the reason ADR-0011 established and ADR-0014
    inherits: every uniqueness check runs under this tenant's policy, so a value
    another tenant holds is never consulted and there is nothing to probe. The
    same raw token in two tenants is two independent blocks.

    The retained fingerprint separates the two cases that look identical from
    inside the tenant -- same identity same operation is a replay, same identity
    different label or token is a conflict -- without depending on the idempotency
    result still existing.
    """
    if (client_id is None) != (token is None):
        raise MalformedToken(
            "client_id and tag_token are supplied together or both omitted"
        )

    if client_id is None:
        minted = secrets.token_urlsafe(CANONICAL_TOKEN_BYTES)
        cur = await conn.execute(
            "insert into blocks.physical_blocks (tenant_id, label, token_hash)"
            f" values (%s, %s, %s) returning {BLOCK_COLUMNS}",
            (tenant_id, label, hash_token(minted)),
        )
        row = await cur.fetchone()
        assert row is not None
        return Block(**row), minted, False

    assert token is not None
    canonical_token_bytes(token)          # refuse a non-canonical spelling first
    token_hash = hash_token(token)
    fingerprint = block_creation_fingerprint(client_id, label, token_hash)

    existing = await _block_by_client_id(conn, client_id)
    if existing is not None:
        return _replay_or_conflict(existing, fingerprint), None, True

    try:
        async with conn.transaction():    # savepoint: a lost race must not abort us
            cur = await conn.execute(
                "insert into blocks.physical_blocks"
                " (tenant_id, label, token_hash, client_id, creation_fingerprint)"
                f" values (%s, %s, %s, %s, %s) returning {BLOCK_COLUMNS}",
                (tenant_id, label, token_hash, client_id, fingerprint),
            )
    except psycopg.errors.UniqueViolation as exc:
        # Losing the insert is a question, not an answer. A simultaneous retry of
        # the SAME operation must converge to a replay; answering 409 would tell a
        # client its own retry conflicted with itself.
        constraint = _violated(exc)
        if constraint == "physical_blocks_tenant_client_id_key":
            winner = await _block_by_client_id(conn, client_id)
            if winner is None:
                raise
            return _replay_or_conflict(winner, fingerprint), None, True
        if constraint == "physical_blocks_tenant_token_hash_key":
            # This token already belongs to a different block in this tenant. Same
            # conflict shape as any other registration conflict -- the caller is
            # not told which of its two values collided.
            raise BlockIdentityConflict(
                "that registration cannot be completed in this tenant; "
                "reconcile against the existing block"
            ) from exc
        raise

    row = await cur.fetchone()
    assert row is not None
    return Block(**row), None, False


async def _block_by_client_id(
    conn: psycopg.AsyncConnection, client_id: UUID
) -> dict | None:
    cur = await conn.execute(
        f"select {BLOCK_COLUMNS}, creation_fingerprint from blocks.physical_blocks"
        " where client_id = %s",
        (client_id,),
    )
    return await cur.fetchone()


def _replay_or_conflict(existing: dict, fingerprint: bytes) -> Block:
    row = dict(existing)
    stored = row.pop("creation_fingerprint", None)
    if stored is None or bytes(stored) != fingerprint:
        raise BlockIdentityConflict(
            "that client_id is already used in this tenant by a different "
            "registration; reconcile against the existing block"
        )
    return Block(**row)


async def list_blocks(conn: psycopg.AsyncConnection) -> list[dict]:
    cur = await conn.execute(
        "select b.id, b.label, b.state::text as state,"
        "       bd.project_id as bound_project_id"
        " from blocks.physical_blocks b"
        " left join blocks.block_bindings bd"
        "   on bd.block_id = b.id and bd.is_current"
        " order by b.created_at"
    )
    return list(await cur.fetchall())


async def resolve_token(conn: psycopg.AsyncConnection, token: str) -> dict:
    """Resolve a tag token to its block and current binding.

    Runs inside the caller's tenant scope, so a token belonging to another tenant
    resolves to nothing here even though tokens are globally unique. The tag says
    *which* block; the bearer token says *who is asking*.
    """
    cur = await conn.execute(
        "select b.id as block_id, b.label, bd.project_id, bd.id as binding_id"
        " from blocks.physical_blocks b"
        " left join blocks.block_bindings bd"
        "   on bd.block_id = b.id and bd.is_current"
        " where b.token_hash = %s and b.state = 'active'",
        (hash_token(token),),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no active block for that tag")
    return row


async def unbind_block(
    conn: psycopg.AsyncConnection, tenant_id: UUID, block_id: UUID
) -> None:
    """End a block's current association without opening another.

    The block stays registered and its tag keeps resolving, to a block with no
    project. That matters physically: a block can be taken out of service without
    being destroyed, and without silently re-pointing at something else the next
    time someone taps it.
    """
    cur = await conn.execute(
        "select 1 from blocks.physical_blocks where id = %s", (block_id,)
    )
    if await cur.fetchone() is None:
        raise NotFound("no such block in this tenant")
    cur = await conn.execute(
        "update blocks.block_bindings set is_current = false, ended_at = now()"
        " where block_id = %s and is_current",
        (block_id,),
    )
    if cur.rowcount == 0:
        raise NotFound("that block has no current binding")
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="block.unbound",
        payload={"blockId": str(block_id)},
    )


async def bind_block(
    conn: psycopg.AsyncConnection, tenant_id: UUID, block_id: UUID, project_id: UUID
) -> dict:
    """Point a block at a project, ending any current binding.

    Rebinding appends; it never rewrites history, so work sessions that ran under
    the previous binding keep their meaning.

    **Fail closed.** Both parents are proven visible in THIS tenant before anything
    is ended or inserted. The earlier order ended the current binding first and
    only then attempted the insert, so a foreign or invented id could destroy a
    real binding and *then* fail -- a side effect committed on behalf of a request
    that was never entitled to be here.

    The check is not belt-and-braces over the foreign keys. `block_id` and
    `project_id` reference globally keyed parents, so a guessed UUID from another
    tenant satisfies the constraint while the child row claims the caller's
    tenant: cross-tenant referential corruption that RLS on the child cannot see.
    Migration 0012 adds the tenant-composite invariant that refuses it in the
    database; this is the layer that gives the caller an answer.

    Absent and foreign are the same answer -- `NotFound`, one 404 body -- because
    distinguishing them is precisely the existence oracle the tenant boundary
    exists to prevent.
    """
    cur = await conn.execute(
        "select"
        " (select count(*) from blocks.physical_blocks where id = %s) as block,"
        " (select count(*) from blocks.projects where id = %s) as project",
        (block_id, project_id),
    )
    seen = await cur.fetchone()
    assert seen is not None
    if not seen["block"] or not seen["project"]:
        raise NotFound("block or project not found in this tenant")

    await conn.execute(
        "update blocks.block_bindings set is_current = false, ended_at = now()"
        " where block_id = %s and is_current",
        (block_id,),
    )
    cur = await conn.execute(
        "insert into blocks.block_bindings (tenant_id, block_id, project_id)"
        " values (%s, %s, %s) returning id, block_id, project_id",
        (tenant_id, block_id, project_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("block or project not found in this tenant")
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="block.bound",
        payload={
            "blockId": str(row["block_id"]),
            "projectId": str(row["project_id"]),
            "bindingId": str(row["id"]),
        },
    )
    return row


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------


async def assert_project_exists(conn: psycopg.AsyncConnection, project_id: UUID) -> None:
    """A child collection of a project that does not exist is 404, not empty.

    The choice matters and could have gone either way. An empty list is the
    lazier read of RLS -- a project in another tenant returns no rows, so the
    handler could just say "nothing here". But then a typo in a project id is
    indistinguishable from a project with no milestones, and a client syncing
    against a stale id would quietly show an empty map forever.

    404 leaks nothing: the caller is already a member of this tenant, and within
    a tenant they may see it does not exist. Between tenants the answer is 403
    before this is ever reached.
    """
    cur = await conn.execute("select 1 from blocks.projects where id = %s", (project_id,))
    if await cur.fetchone() is None:
        raise NotFound("no such project in this tenant")


async def assert_session_exists(conn: psycopg.AsyncConnection, session_id: UUID) -> None:
    cur = await conn.execute("select 1 from blocks.work_sessions where id = %s", (session_id,))
    if await cur.fetchone() is None:
        raise NotFound("no such work session in this tenant")


async def list_map(conn: psycopg.AsyncConnection, project_id: UUID) -> list[dict]:
    cur = await conn.execute(
        "select id, title, status::text as status, sort_key, row_version"
        " from blocks.map_items where project_id = %s order by sort_key",
        (project_id,),
    )
    return list(await cur.fetchall())


async def add_map_item(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    project_id: UUID,
    title: str,
    after_id: UUID | None = None,
) -> dict:
    """Append, or insert immediately after `after_id`."""
    if after_id is None:
        cur = await conn.execute(
            "select coalesce(max(sort_key), 0) + %s as k from blocks.map_items"
            " where project_id = %s",
            (SORT_GAP, project_id),
        )
        row = await cur.fetchone()
        sort_key = row["k"]
    else:
        cur = await conn.execute(
            "select sort_key from blocks.map_items where id = %s and project_id = %s",
            (after_id, project_id),
        )
        anchor = await cur.fetchone()
        if anchor is None:
            raise NotFound("no such map item in this project")
        cur = await conn.execute(
            "select min(sort_key) as k from blocks.map_items"
            " where project_id = %s and sort_key > %s",
            (project_id, anchor["sort_key"]),
        )
        following = (await cur.fetchone())["k"]
        sort_key = (
            anchor["sort_key"] + SORT_GAP
            if following is None
            else (anchor["sort_key"] + following) // 2
        )
        if sort_key in (anchor["sort_key"], following):
            # The gap is exhausted. Renumbering is the correct fix and is not
            # implemented, so fail loudly rather than silently reorder.
            raise ConcurrencyConflict("ordering gap exhausted; this list needs renumbering")

    cur = await conn.execute(
        "insert into blocks.map_items (tenant_id, project_id, title, sort_key)"
        " values (%s, %s, %s, %s)"
        " returning id, title, status::text as status, sort_key, row_version",
        (tenant_id, project_id, title, sort_key),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def update_map_item(
    conn: psycopg.AsyncConnection,
    item_id: UUID,
    expected_version: int,
    *,
    title: str | None = None,
    status: str | None = None,
) -> dict:
    """Optimistic concurrency: the caller states the version it read."""
    cur = await conn.execute(
        "update blocks.map_items"
        " set title = coalesce(%s, title),"
        "     status = coalesce(%s, status),"
        "     row_version = row_version + 1,"
        "     updated_at = now()"
        " where id = %s and row_version = %s"
        " returning id, title, status::text as status, sort_key, row_version",
        (title, status, item_id, expected_version),
    )
    row = await cur.fetchone()
    if row is None:
        raise ConcurrencyConflict(
            "map item was modified by someone else, or does not exist in this tenant"
        )
    return row


# ---------------------------------------------------------------------------
# Work sessions and the parking contract
# ---------------------------------------------------------------------------


async def start_session(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    *,
    project_id: UUID,
    actor_id: UUID,
    core_outcome: str,
    full_outcome: str | None = None,
    polish_outcome: str | None = None,
    boundary_at: datetime | None = None,
    physical_block_id: UUID | None = None,
) -> dict:
    try:
        cur = await conn.execute(
            "insert into blocks.work_sessions"
            " (tenant_id, project_id, physical_block_id, started_by_user_id,"
            "  boundary_at, core_outcome, full_outcome, polish_outcome)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)"
            " returning id, project_id, physical_block_id, state::text as state,"
            "           core_outcome, full_outcome, polish_outcome, boundary_at, row_version",
            (
                tenant_id, project_id, physical_block_id, actor_id,
                boundary_at, core_outcome, full_outcome, polish_outcome,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        # The partial unique index on (started_by_user_id) where state='active'.
        # Enforced by the database so two devices racing offline cannot both win.
        raise ActiveSessionExists(
            "you already have an active session; park or close it first"
        ) from exc
    row = await cur.fetchone()
    assert row is not None
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="session.started",
        payload={
            # Identifiers only. The core outcome is user-authored text and stays
            # on the resource: an event payload may travel through transports and
            # sinks this service does not control.
            "sessionId": str(row["id"]),
            "projectId": str(row["project_id"]),
            **({"blockId": str(row["physical_block_id"])} if row["physical_block_id"] else {}),
            **({"boundaryAt": row["boundary_at"].isoformat()} if row["boundary_at"] else {}),
        },
    )
    return row


async def _transition(
    conn: psycopg.AsyncConnection,
    session_id: UUID,
    expected_version: int,
    to_state: str,
    *,
    stamp: str,
) -> dict:
    cur = await conn.execute(
        "select state::text as state, row_version from blocks.work_sessions where id = %s",
        (session_id,),
    )
    current = await cur.fetchone()
    if current is None:
        raise NotFound("no such work session in this tenant")
    if to_state not in TRANSITIONS[current["state"]]:
        raise IllegalTransition(
            f"a {current['state']} session cannot become {to_state}"
        )

    cur = await conn.execute(
        f"update blocks.work_sessions"
        f" set state = %s, {stamp} = now(), row_version = row_version + 1, updated_at = now()"
        f" where id = %s and row_version = %s"
        f" returning id, project_id, state::text as state, row_version",
        (to_state, session_id, expected_version),
    )
    row = await cur.fetchone()
    if row is None:
        raise ConcurrencyConflict("work session was modified by someone else")
    return row


async def _append_parking(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    session_id: UUID,
    *,
    actor_id: UUID,
    where_i_am: str,
    next_physical_action: str,
    must_not_forget: str,
    device_id: UUID | None = None,
) -> dict:
    """Append one immutable parking record. Shared by park and by atomic close."""
    cur = await conn.execute(
        "select coalesce(max(sequence), 0) + 1 as n from blocks.parking_records"
        " where work_session_id = %s",
        (session_id,),
    )
    sequence = (await cur.fetchone())["n"]
    # project_id is derived from the session rather than passed in: parking now
    # belongs to a project (ADR-0009), and a 0.2 client knows nothing about that.
    # Deriving it here is what keeps the 0.2 endpoints working through the
    # declared compatibility window instead of failing on a NOT NULL they cannot
    # see.
    cur = await conn.execute(
        "insert into blocks.parking_records"
        " (tenant_id, work_session_id, project_id, where_i_am, next_physical_action,"
        "  must_not_forget, authored_by_user_id, device_id, sequence)"
        " select %s, %s, w.project_id, %s, %s, %s, %s, %s, %s"
        "   from blocks.work_sessions w where w.id = %s"
        " returning id, where_i_am, next_physical_action, must_not_forget, sequence, created_at",
        (
            tenant_id, session_id, where_i_am, next_physical_action, must_not_forget,
            actor_id, device_id, sequence, session_id,
        ),
    )
    record = await cur.fetchone()
    assert record is not None
    return record


async def park_session(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    session_id: UUID,
    expected_version: int,
    *,
    actor_id: UUID,
    where_i_am: str,
    next_physical_action: str,
    must_not_forget: str,
    device_id: UUID | None = None,
) -> tuple[dict, dict]:
    """Park a session, appending the three-question record.

    The record and the transition commit together. A parked session without its
    parking record would be exactly the "silent half-finished state" invariant 3
    forbids, so it is not a state this can produce.
    """
    session = await _transition(conn, session_id, expected_version, "parked", stamp="parked_at")
    record = await _append_parking(
        conn, tenant_id, session_id,
        actor_id=actor_id, where_i_am=where_i_am,
        next_physical_action=next_physical_action, must_not_forget=must_not_forget,
        device_id=device_id,
    )

    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="session.parked",
        payload={
            # The three parking answers are the most sensitive text in the
            # product. They are not put in an event; the record id is enough for
            # a consumer to fetch them over an authenticated channel.
            "sessionId": str(session_id),
            "parkingRecordId": str(record["id"]),
        },
    )
    return session, record


async def resume_session(
    conn: psycopg.AsyncConnection, tenant_id: UUID, session_id: UUID, expected_version: int
) -> dict:
    try:
        session = await _transition(
            conn, session_id, expected_version, "active", stamp="resumed_at"
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ActiveSessionExists(
            "you already have another active session; park or close it first"
        ) from exc
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="session.resumed",
        payload={"sessionId": str(session_id)},
    )
    return session


async def close_session(
    conn: psycopg.AsyncConnection,
    tenant_id: UUID,
    session_id: UUID,
    expected_version: int,
    outcome_tier: str | None = None,
    interruption: str | None = None,
    *,
    actor_id: UUID | None = None,
    continuation: dict | None = None,
) -> tuple[dict, dict | None]:
    """Close a session and record what it measured.

    Two forms, and the second exists because of a real offline failure Android
    found. A client that queues "park" then "close" while offline cannot supply a
    valid `If-Match` on the close: the park advances the row version, and the
    queued close was written before that version existed. Sending the pre-park
    version is stale, omitting it is invalid, and replaying close first fails
    because no continuation exists yet. FIFO ordering does not help -- the
    dependency is on a value that did not exist when the intent was recorded.

    So "preserve this continuation and close" is one operation, guarded by the one
    version the client actually knows. The parking record is appended and the
    session closed in a single transaction: if the version is stale, the update
    matches no row, the whole transaction rolls back, and NOTHING is written --
    not even the parking record.

    The calibration event is written in the same transaction too. Not durability
    theatre -- evidence gathered later is evidence reconstructed, and invariant 8
    only works if the numbers are observations.
    """
    record = None
    if continuation is not None:
        if outcome_tier is not None:
            raise IllegalTransition(
                "close takes either an outcome tier or a continuation, not both"
            )
        assert actor_id is not None, "a continuation must have an author"
        # Appended BEFORE the versioned update on purpose: if that update finds no
        # row because the version is stale, this insert rolls back with it.
        record = await _append_parking(
            conn, tenant_id, session_id, actor_id=actor_id, **continuation
        )
    elif outcome_tier is None:
        # Invariant 3: a boundary must preserve a completed core OR an explicitly
        # parked continuation. Closing with neither is exactly the "silent
        # half-finished state" the constitution refuses, so it is refused here
        # rather than accepted and quietly lost.
        cur = await conn.execute(
            "select 1 from blocks.parking_records where work_session_id = %s limit 1",
            (session_id,),
        )
        if await cur.fetchone() is None:
            raise IllegalTransition(
                "closing without an outcome tier requires an explicitly parked "
                "continuation: park the session first, or state the tier reached"
            )

    session = await _transition(conn, session_id, expected_version, "closed", stamp="closed_at")

    cur = await conn.execute(
        "update blocks.work_sessions set outcome_tier = %s, interruption = %s"
        " where id = %s"
        " returning started_at, closed_at, parked_at, resumed_at,"
        "           full_outcome, polish_outcome",
        (outcome_tier, interruption or "boundary", session_id),
    )
    detail = await cur.fetchone()
    assert detail is not None

    # The highest tier the session actually committed to, which is what
    # "planned versus actual" has to compare against to mean anything.
    planned = "polish" if detail["polish_outcome"] else ("full" if detail["full_outcome"] else "core")
    elapsed = int((detail["closed_at"] - detail["started_at"]).total_seconds())
    latency = None
    if detail["parked_at"] and detail["resumed_at"] and detail["resumed_at"] > detail["parked_at"]:
        latency = int((detail["resumed_at"] - detail["parked_at"]).total_seconds())

    await conn.execute(
        "insert into blocks.calibration_events"
        " (tenant_id, work_session_id, planned_tier, actual_tier, elapsed_seconds,"
        "  interruption, restart_latency_seconds)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, session_id, planned, outcome_tier, elapsed,
         interruption or "boundary", latency),
    )

    if record is not None:
        # Both facts happened, so both are announced -- and the parked event
        # carries the record id, never the three answers.
        await events.emit(
            conn,
            tenant_id=tenant_id,
            event_type="session.parked",
            payload={"sessionId": str(session_id), "parkingRecordId": str(record["id"])},
        )
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="session.closed",
        payload={"sessionId": str(session_id), "outcomeTier": outcome_tier},
    )
    return session, record


async def calibration_for_session(conn: psycopg.AsyncConnection, session_id: UUID) -> list[dict]:
    cur = await conn.execute(
        "select id, planned_tier::text as planned_tier, actual_tier::text as actual_tier,"
        "       elapsed_seconds, interruption::text as interruption,"
        "       restart_latency_seconds, created_at"
        " from blocks.calibration_events where work_session_id = %s order by created_at",
        (session_id,),
    )
    return list(await cur.fetchall())


async def calibration_for_tenant(conn: psycopg.AsyncConnection, limit: int = 100) -> list[dict]:
    """Raw observations, newest first.

    Deliberately returns rows and no aggregate. There is no completion rate here,
    no streak, and no total, because invariant 8 forbids presenting these as a
    score and the cheapest way to honour that is to have nothing to present.
    """
    cur = await conn.execute(
        "select id, work_session_id, planned_tier::text as planned_tier,"
        "       actual_tier::text as actual_tier, elapsed_seconds,"
        "       interruption::text as interruption, restart_latency_seconds, created_at"
        " from blocks.calibration_events order by created_at desc limit %s",
        (limit,),
    )
    return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# Map dependencies
# ---------------------------------------------------------------------------


async def list_dependencies(conn: psycopg.AsyncConnection, project_id: UUID) -> list[dict]:
    cur = await conn.execute(
        "select d.predecessor_id, d.successor_id from blocks.map_dependencies d"
        " join blocks.map_items p on p.id = d.predecessor_id"
        " where p.project_id = %s order by d.predecessor_id, d.successor_id",
        (project_id,),
    )
    return list(await cur.fetchall())


async def add_dependency(
    conn: psycopg.AsyncConnection, tenant_id: UUID, predecessor_id: UUID, successor_id: UUID
) -> dict:
    """Link two milestones, refusing anything that would create a cycle.

    Self-dependency is caught by a check constraint; longer cycles cannot be, so
    they are caught here with a reachability walk. A cycle in a map is not a
    validation nicety -- it is a map that can never be ordered, which is the one
    thing the map has to be.
    """
    cur = await conn.execute(
        "select project_id from blocks.map_items where id = any(%s)",
        ([predecessor_id, successor_id],),
    )
    projects = {row["project_id"] for row in await cur.fetchall()}
    if len(projects) != 1:
        raise NotFound("both items must exist, in the same project, in this tenant")

    # Would the successor already reach the predecessor? If so, adding this edge
    # closes a loop.
    cur = await conn.execute(
        "with recursive reachable(id) as ("
        "   select successor_id from blocks.map_dependencies where predecessor_id = %s"
        "   union"
        "   select d.successor_id from blocks.map_dependencies d"
        "     join reachable r on r.id = d.predecessor_id"
        ") select 1 from reachable where id = %s limit 1",
        (successor_id, predecessor_id),
    )
    if await cur.fetchone() is not None:
        raise IllegalTransition(
            "that dependency would create a cycle; the map must stay orderable"
        )

    try:
        cur = await conn.execute(
            "insert into blocks.map_dependencies (tenant_id, predecessor_id, successor_id)"
            " values (%s, %s, %s) returning predecessor_id, successor_id",
            (tenant_id, predecessor_id, successor_id),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise IllegalTransition("that dependency already exists") from exc
    except psycopg.errors.CheckViolation as exc:
        raise IllegalTransition("a milestone cannot depend on itself") from exc
    row = await cur.fetchone()
    assert row is not None
    return row


async def remove_dependency(
    conn: psycopg.AsyncConnection, predecessor_id: UUID, successor_id: UUID
) -> None:
    cur = await conn.execute(
        "delete from blocks.map_dependencies"
        " where predecessor_id = %s and successor_id = %s",
        (predecessor_id, successor_id),
    )
    if cur.rowcount == 0:
        raise NotFound("no such dependency in this tenant")


async def newest_parking(conn: psycopg.AsyncConnection, session_id: UUID) -> dict | None:
    """The record re-entry shows: newest, and not itself superseded."""
    cur = await conn.execute(
        "select p.id, p.where_i_am, p.next_physical_action, p.must_not_forget,"
        "       p.sequence, p.created_at"
        " from blocks.parking_records p"
        " where p.work_session_id = %s"
        "   and not exists (select 1 from blocks.parking_records s where s.supersedes_id = p.id)"
        " order by p.sequence desc limit 1",
        (session_id,),
    )
    return await cur.fetchone()


async def parking_history(conn: psycopg.AsyncConnection, session_id: UUID) -> list[dict]:
    cur = await conn.execute(
        "select id, where_i_am, next_physical_action, must_not_forget, sequence,"
        "       supersedes_id, created_at"
        " from blocks.parking_records where work_session_id = %s order by sequence",
        (session_id,),
    )
    return list(await cur.fetchall())


#: The columns the re-entry surface exposes, in one place so both branches of the
#: lookup below cannot drift apart.
_SESSION_COLUMNS = (
    "id, project_id, physical_block_id, state::text as state, core_outcome,"
    " full_outcome, polish_outcome, boundary_at, row_version, started_at"
)


async def current_session(conn: psycopg.AsyncConnection, actor_id: UUID) -> dict | None:
    """The re-entry view, in precedence order.

    Constitution invariant 2 -- restarting after time away must require no audit
    -- and invariant 4 -- *re-entry must make the newest parking record
    immediately available*.

    1. the active session, if there is one. At most one exists per user, and it
       is where the person actually is;
    2. otherwise the session owning the **globally newest non-superseded parking
       record**, whether that session is parked or closed.

    Step 2 deliberately does not privilege `parked` over `closed`. An earlier
    version did, and it was wrong: park session A, then atomically park-and-close
    session B, and A would win on state alone while hiding B's newer record --
    which is precisely the record invariant 4 promises. State is not recency, and
    only recency was ever the question here.

    A parked session always has at least one record (parking appends one), so
    dropping the state tier loses nothing: a parked session competes on the age of
    its own record, like everything else. A session closed on a completed outcome
    tier has no record and correctly never appears.
    """
    cur = await conn.execute(
        f"select {_SESSION_COLUMNS}"
        " from blocks.work_sessions"
        " where started_by_user_id = %s and state = 'active'"
        " limit 1",
        (actor_id,),
    )
    active = await cur.fetchone()
    if active is not None:
        return active

    cur = await conn.execute(
        f"select {_SESSION_COLUMNS}"
        " from blocks.work_sessions w"
        " join lateral ("
        "   select p.created_at, p.sequence"
        "   from blocks.parking_records p"
        "   where p.work_session_id = w.id"
        "     and not exists ("
        "       select 1 from blocks.parking_records s where s.supersedes_id = p.id"
        "     )"
        "   order by p.created_at desc, p.sequence desc"
        "   limit 1"
        " ) newest on true"
        " where w.started_by_user_id = %s"
        " order by newest.created_at desc, newest.sequence desc, w.started_at desc"
        " limit 1",
        (actor_id,),
    )
    return await cur.fetchone()
