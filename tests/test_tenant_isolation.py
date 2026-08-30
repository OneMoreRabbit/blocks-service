"""Cross-tenant isolation -- the acceptance gate for the walking skeleton.

Asserted at both levels on purpose. The API tests prove the service denies the
request; the database tests prove that isolation does not *depend* on the service
getting it right. A future handler that forgets a tenant filter, or a query built
by a tool that never learned about tenancy, still returns nothing.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from blocks_service.db import Database


async def _session(client, auth, subject: str) -> dict:
    response = await client.post("/v1/session", headers=auth(subject))
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


async def test_a_member_can_create_and_list_projects_in_their_own_tenant(client, auth):
    alice = await _session(client, auth, "alice")
    tenant = alice["personal_tenant_id"]

    created = await client.post(
        f"/v1/tenants/{tenant}/projects",
        headers=auth("alice"),
        json={"name": "Kitchen rewire"},
    )
    assert created.status_code == 201
    assert created.json()["tenant_id"] == tenant

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["Kitchen rewire"]


async def test_a_non_member_cannot_read_another_tenants_projects(client, auth):
    alice = await _session(client, auth, "alice")
    await _session(client, auth, "bob")
    await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": "Private"},
    )

    response = await client.get(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects", headers=auth("bob")
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "unauthorized"
    assert "Private" not in response.text


async def test_a_non_member_cannot_write_into_another_tenant(client, auth):
    alice = await _session(client, auth, "alice")
    await _session(client, auth, "bob")

    response = await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("bob"),
        json={"name": "Injected"},
    )
    assert response.status_code == 403

    listed = await client.get(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects", headers=auth("alice")
    )
    assert listed.json() == []


async def test_an_unknown_tenant_is_indistinguishable_from_a_forbidden_one(client, auth):
    """Both must be 403, or tenant ids become enumerable."""
    await _session(client, auth, "alice")
    absent = await client.get(f"/v1/tenants/{uuid4()}/projects", headers=auth("alice"))
    assert absent.status_code == 403
    assert absent.json()["error"]["code"] == "unauthorized"


async def test_projects_require_authentication(client, auth):
    alice = await _session(client, auth, "alice")
    response = await client.get(f"/v1/tenants/{alice['personal_tenant_id']}/projects")
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Directly against PostgreSQL, as the unprivileged runtime role
# --------------------------------------------------------------------------


async def test_rls_hides_rows_from_a_transaction_scoped_to_another_tenant(
    client, auth, db: Database
):
    alice = await _session(client, auth, "alice")
    bob = await _session(client, auth, "bob")
    await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": "Alice only"},
    )

    async with db.tenant_transaction(bob["personal_tenant_id"], bob["user_id"]) as conn:
        # No WHERE clause at all: whatever comes back is what the policy allows.
        cur = await conn.execute("select name from blocks.projects")
        assert await cur.fetchall() == []

    async with db.tenant_transaction(alice["personal_tenant_id"], alice["user_id"]) as conn:
        cur = await conn.execute("select name from blocks.projects")
        assert [r["name"] for r in await cur.fetchall()] == ["Alice only"]


async def test_a_transaction_with_no_tenant_context_sees_nothing(client, auth, db: Database):
    """Fail closed: forgetting to scope must yield zero rows, not every row."""
    alice = await _session(client, auth, "alice")
    await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": "Alice only"},
    )

    async with db.transaction() as conn:
        cur = await conn.execute("select name from blocks.projects")
        assert await cur.fetchall() == []
        cur = await conn.execute("select tenant_id from blocks.memberships")
        assert await cur.fetchall() == []


async def test_the_write_check_refuses_a_row_belonging_to_another_tenant(
    client, auth, db: Database
):
    """USING hides; WITH CHECK is what stops a scoped session planting a row elsewhere."""
    alice = await _session(client, auth, "alice")
    bob = await _session(client, auth, "bob")

    async with db.tenant_transaction(bob["personal_tenant_id"], bob["user_id"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                "insert into blocks.projects (tenant_id, name) values (%s, %s)",
                (alice["personal_tenant_id"], "Planted"),
            )


async def test_tenant_context_does_not_survive_its_transaction(client, auth, db: Database):
    """The pooled-connection failure mode, asserted rather than assumed.

    A context installed with SET LOCAL must be gone once the transaction ends,
    or the next borrower of that connection inherits it.
    """
    alice = await _session(client, auth, "alice")

    async with db.tenant_transaction(alice["personal_tenant_id"], alice["user_id"]) as conn:
        cur = await conn.execute("select blocks.current_tenant() as t")
        assert (await cur.fetchone())["t"] is not None

    async with db.transaction() as conn:
        cur = await conn.execute("select blocks.current_tenant() as t")
        assert (await cur.fetchone())["t"] is None
