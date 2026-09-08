"""Request-scoped dependencies: authentication, then tenant scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
from fastapi import Depends, Header, Path, Request

from .. import errors, repository
from ..db import Database
from ..identity import IdentityVerifier, InvalidToken, Principal
from ..idempotency import Idempotency, creation_identity, fingerprint
from ..operating_loop import (
    ActiveSessionExists,
    ConcurrencyConflict,
    DomainError,
    IllegalTransition,
    NotFound,
)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_verifier(request: Request) -> IdentityVerifier:
    return request.app.state.verifier


def verified_principal(
    request: Request, verifier: IdentityVerifier = Depends(get_verifier)
) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise errors.unauthenticated()
    try:
        return verifier.verify(token.strip())
    except InvalidToken as exc:
        raise errors.unauthenticated(f"Token rejected: {exc}") from exc


async def current_user_id(
    principal: Principal = Depends(verified_principal),
    db: Database = Depends(get_db),
) -> UUID:
    """The local user for a verified principal, which must already exist.

    Only POST /v1/session creates users. Every other route requires that
    bootstrap to have happened, so a token for an unknown subject is
    unauthenticated here rather than silently provisioning an account.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            "select id from blocks.users where issuer = %s and subject = %s",
            (principal.issuer, principal.subject),
        )
        row = await cur.fetchone()
    if row is None:
        raise errors.unauthenticated("Unknown subject; call POST /v1/session first.")
    return row["id"]


@dataclass(slots=True)
class TenantContext:
    """A transaction already scoped to a tenant the caller belongs to."""

    conn: psycopg.AsyncConnection
    tenant_id: UUID
    actor_id: UUID
    role: str


async def tenant_context(
    tenant_id: UUID = Path(...),
    actor_id: UUID = Depends(current_user_id),
    db: Database = Depends(get_db),
) -> AsyncIterator[TenantContext]:
    """Open a tenant-scoped transaction and prove membership inside it.

    Scope first, authorize second, and both against the same context: the
    membership probe is itself subject to the policy it is authorizing, so there
    is no window in which a query runs with the tenant claimed but not checked.
    """
    async with db.tenant_transaction(tenant_id, actor_id) as conn:
        role = await repository.membership_role(conn, actor_id)
        if role is None:
            raise errors.unauthorized()
        yield TenantContext(conn=conn, tenant_id=tenant_id, actor_id=actor_id, role=role)


async def idempotency(
    request: Request,
    ctx: "TenantContext" = Depends(tenant_context),
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        description=(
            "Send one per distinct operation your offline queue may retry. The "
            "outcome is stored and replayed verbatim, with `Idempotent-Replay: "
            "true` on the repeat. Reusing a key with a different body is `409`."
        ),
    ),
) -> Idempotency:
    """Bind an `Idempotency-Key`, if the client sent one, to this transaction.

    Declared as a `Header` parameter rather than read off the raw request so it
    appears in the generated description — a header a client must send is part of
    the machine contract, not a convention documented in prose.
    """
    body = await request.body()
    idem = Idempotency(
        ctx.conn,
        ctx.tenant_id,
        idempotency_key,
        fingerprint(request.method, request.url.path, body),
    )
    # Carried alongside so a handler creating a resource can retain the identity
    # that survives result expiry (ADR-0011). Same key + same initial body.
    idem.creation_identity = creation_identity(
        idempotency_key, request.method, request.url.path, body
    )
    return idem


def if_match_version(
    if_match: str = Header(
        ...,
        alias="If-Match",
        description=(
            "**Required.** The `row_version` you read, as an integer. A stale "
            "value is `409`; a missing one is `400`."
        ),
    ),
) -> int:
    """The `row_version` the client believes it is acting on.

    Required on every state transition, deliberately. Making it optional would
    let a client that has never heard of concurrency overwrite a change it never
    saw -- and offline clients are exactly the ones that will not have seen it.
    """
    raw = if_match
    try:
        return int(raw.strip().strip('"').lstrip("W/").strip('"'))
    except ValueError:
        raise errors.invalid_request(
            f"If-Match must be the integer row_version, not {raw!r}."
        ) from None


def as_http(exc: DomainError) -> errors.ApiError:
    """Map a domain rule to the contract's error codes.

    `NotFound` becomes 403, not 404: inside a tenant the caller belongs to, a
    missing row and a row in another tenant are indistinguishable *by design* --
    RLS returns nothing for both -- so reporting 404 would leak the difference.
    """
    if isinstance(exc, NotFound):
        return errors.not_found(str(exc))
    if isinstance(exc, (ConcurrencyConflict, IllegalTransition, ActiveSessionExists)):
        return errors.conflict(str(exc))
    return errors.invalid_request(str(exc))


def _parse_version(raw: str) -> tuple[int, ...]:
    parts = raw.strip().split("-", 1)[0].split(".")
    return tuple(int(p) for p in parts[:3])


def require_supported_client(
    request: Request,
    x_blocks_client_version: str | None = Header(
        None,
        alias="X-Blocks-Client-Version",
        description=(
            "Optional dotted version. Below the declared minimum is `426`; "
            "absent is allowed; unparseable is `400`."
        ),
    ),
) -> None:
    """Refuse clients below the declared minimum.

    A client that sends no `X-Blocks-Client-Version` is allowed through — the
    header is additive, and refusing on its absence would break every client that
    predates it. An unparseable value IS refused: "cannot tell" is not the same
    as "old enough", and guessing in the client's favour is the wrong default for
    a gate that exists to stop incompatible writes.
    """
    raw = x_blocks_client_version
    if raw is None:
        return
    settings = request.app.state.settings
    try:
        if _parse_version(raw) < _parse_version(settings.min_client_version):
            raise errors.upgrade_required(
                f"Client {raw} is below the minimum supported {settings.min_client_version}."
            )
    except ValueError:
        raise errors.invalid_request(
            f"X-Blocks-Client-Version must be a dotted version, not {raw!r}."
        ) from None
