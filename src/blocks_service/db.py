"""Database access and the transaction-local tenant boundary.

The only supported way to touch tenant-scoped data is `tenant_transaction`. It
opens a transaction, installs the tenant and actor context with `set_config(...,
is_local => true)`, and hands back the connection. Because the context is
transaction-local it is discarded on commit or rollback, so a connection cannot
carry one tenant's context into the next borrower of the pool -- the failure mode
that makes pooled connections and RLS a classic bad pairing.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class RlsBypassError(RuntimeError):
    """The configured runtime role can bypass row-level security."""


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self._dsn = dsn
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=10)

    async def close(self) -> None:
        await self._pool.close()

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """A transaction with NO tenant context.

        Legitimate only for the identity plane (`users`, `tenants`) and for
        liveness/readiness probes. Every tenant-scoped table has a policy that
        compares against a NULL context here, so this connection sees none of
        their rows -- by construction, not by convention.
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                yield conn

    @contextlib.asynccontextmanager
    async def tenant_transaction(
        self, tenant_id: UUID, actor_id: UUID
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await set_transaction_context(conn, tenant_id=tenant_id, actor_id=actor_id)
                yield conn

    async def assert_cannot_bypass_rls(self) -> None:
        """Fail loudly if the runtime role is exempt from the policies.

        A role that acquires SUPERUSER or BYPASSRLS turns every policy in the
        schema into a comment. That is silent and total, so it is checked on
        every readiness probe rather than trusted from provisioning time.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
            )
            row = await cur.fetchone()
        assert_role_cannot_bypass(row)

    async def ping(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("select 1")


async def set_transaction_context(
    conn: psycopg.AsyncConnection, *, tenant_id: UUID, actor_id: UUID
) -> None:
    """Install the tenant/actor context on the CURRENT transaction.

    `set_config(..., is_local => true)` rather than `SET LOCAL` because the
    values are bound parameters -- a tenant id is never interpolated into SQL
    text. Exposed separately from `tenant_transaction` for the one flow that
    cannot know its tenant up front: first-login bootstrap creates the personal
    tenant and then adopts its context, inside a single transaction, so a user
    with no tenant is never committed.
    """
    await conn.execute(
        "select set_config('blocks.tenant_id', %s, true),"
        "       set_config('blocks.user_id',   %s, true)",
        (str(tenant_id), str(actor_id)),
    )


def assert_role_cannot_bypass(row: dict | None) -> None:
    """Raise unless the role described by `row` is subject to row-level security.

    Split out from the query so the decision can be tested directly. Since the
    estate rotated its superuser away, no role reachable from this service can
    bypass RLS -- which is the point, and also means the negative case has no
    live role left to exercise it. A guard whose failure path is untested is a
    guard nobody has seen work.
    """
    if row is None:
        raise RlsBypassError("runtime role not found in pg_roles")
    if row["rolsuper"] or row["rolbypassrls"]:
        raise RlsBypassError(
            "runtime role has SUPERUSER or BYPASSRLS; tenant isolation is not enforced"
        )
