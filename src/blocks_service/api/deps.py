"""Request-scoped dependencies: authentication, then tenant scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
from fastapi import Depends, Path, Request

from .. import errors, repository
from ..db import Database
from ..identity import IdentityVerifier, InvalidToken, Principal


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
