"""Persistence for the identity plane, tenancy, and the first domain entity.

Split by trust boundary rather than by entity. `bootstrap_session` runs without
tenant context because it is what *produces* one; everything below it requires a
caller that has already been scoped, so no query in this module has to remember
to filter by tenant -- the database does it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg

from . import events
from .db import Database, set_transaction_context
from .identity import Principal


@dataclass(frozen=True, slots=True)
class Tenant:
    id: UUID
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class Session:
    user_id: UUID
    email: str | None
    tenants: list[Tenant]
    personal_tenant_id: UUID


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    tenant_id: UUID
    name: str


def _personal_tenant_name(principal: Principal) -> str:
    return f"{principal.email or principal.subject}'s blocks"


async def bootstrap_session(db: Database, principal: Principal) -> Session:
    """Resolve the verified principal to a user, creating them on first login.

    First login creates the user, their one personal tenant, and the owner
    membership joining them -- in a single transaction. A user without a tenant,
    or a personal tenant without its owner, is never a state that can be
    observed or left behind by a crash midway.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            "select id, email from blocks.users where issuer = %s and subject = %s",
            (principal.issuer, principal.subject),
        )
        row = await cur.fetchone()

        if row is None:
            cur = await conn.execute(
                "insert into blocks.users (issuer, subject, email) values (%s, %s, %s)"
                " returning id, email",
                (principal.issuer, principal.subject, principal.email),
            )
            row = await cur.fetchone()
            assert row is not None
            user_id = row["id"]

            cur = await conn.execute(
                "insert into blocks.tenants (kind, name, personal_for)"
                " values ('personal', %s, %s) returning id",
                (_personal_tenant_name(principal), user_id),
            )
            tenant_row = await cur.fetchone()
            assert tenant_row is not None
            personal_tenant_id = tenant_row["id"]

            # memberships is under forced RLS, so the insert is only permitted
            # once this transaction has adopted the new tenant's context.
            await set_transaction_context(
                conn, tenant_id=personal_tenant_id, actor_id=user_id
            )
            await conn.execute(
                "insert into blocks.memberships (tenant_id, user_id, role)"
                " values (%s, %s, 'owner')",
                (personal_tenant_id, user_id),
            )
        else:
            user_id = row["id"]

        cur = await conn.execute(
            "select id, kind::text as kind, name, personal_for"
            " from blocks.tenants where personal_for = %s",
            (user_id,),
        )
        personal = await cur.fetchone()
        assert personal is not None, "every user has exactly one personal tenant"

    tenants = await _tenants_for_user(db, user_id)
    return Session(
        user_id=user_id,
        email=row["email"],
        tenants=tenants,
        personal_tenant_id=personal["id"],
    )


async def _tenants_for_user(db: Database, user_id: UUID) -> list[Tenant]:
    """List the tenants a user belongs to.

    Membership rows are readable only inside their own tenant's context, so this
    walks the identity plane (`tenants.personal_for`) plus one scoped probe per
    candidate tenant rather than joining across the boundary. For 0.1 a user has
    exactly one tenant; organization tenancy is out of MVP scope, and when it
    arrives this is the query that changes.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            "select id, kind::text as kind, name from blocks.tenants"
            " where personal_for = %s order by created_at",
            (user_id,),
        )
        rows = await cur.fetchall()
    return [Tenant(id=r["id"], kind=r["kind"], name=r["name"]) for r in rows]


async def membership_role(conn: psycopg.AsyncConnection, actor_id: UUID) -> str | None:
    """The actor's role in the tenant this transaction is scoped to, or None.

    No tenant predicate: RLS supplies it. A non-member -- and a member of some
    *other* tenant asking about this one -- both see zero rows, and neither can
    tell from the result whether the tenant exists.
    """
    cur = await conn.execute(
        "select role::text as role from blocks.memberships where user_id = %s",
        (actor_id,),
    )
    row = await cur.fetchone()
    return row["role"] if row else None


async def list_projects(conn: psycopg.AsyncConnection) -> list[Project]:
    cur = await conn.execute(
        "select id, tenant_id, name from blocks.projects order by created_at"
    )
    return [Project(**row) for row in await cur.fetchall()]


async def create_project(
    conn: psycopg.AsyncConnection, tenant_id: UUID, name: str
) -> Project:
    cur = await conn.execute(
        "insert into blocks.projects (tenant_id, name) values (%s, %s)"
        " returning id, tenant_id, name",
        (tenant_id, name),
    )
    row = await cur.fetchone()
    assert row is not None
    project = Project(**row)
    # Same transaction as the insert above: the project and the news of it
    # commit together or neither does.
    await events.emit(
        conn,
        tenant_id=tenant_id,
        event_type="project.created",
        payload={"projectId": str(project.id), "name": project.name},
    )
    return project
