"""First-login bootstrap: one user, one personal tenant, one owner membership."""

from __future__ import annotations

import psycopg
import pytest

from blocks_service.identity import DEV_ISSUER


async def test_first_login_creates_user_and_personal_tenant(client, auth):
    response = await client.post("/v1/session", headers=auth("alice", "alice@example.test"))
    assert response.status_code == 200

    body = response.json()
    assert body["email"] == "alice@example.test"
    assert body["personal_tenant_id"] is not None
    assert [t["kind"] for t in body["tenants"]] == ["personal"]
    assert body["tenants"][0]["id"] == body["personal_tenant_id"]


async def test_bootstrap_is_idempotent(client, auth):
    first = await client.post("/v1/session", headers=auth("alice"))
    second = await client.post("/v1/session", headers=auth("alice"))
    assert first.status_code == second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.json()["personal_tenant_id"] == second.json()["personal_tenant_id"]


async def test_distinct_subjects_get_distinct_tenants(client, auth):
    alice = (await client.post("/v1/session", headers=auth("alice"))).json()
    bob = (await client.post("/v1/session", headers=auth("bob"))).json()
    assert alice["user_id"] != bob["user_id"]
    assert alice["personal_tenant_id"] != bob["personal_tenant_id"]


async def test_a_user_cannot_have_a_second_personal_tenant(client, auth, settings):
    """The invariant is the database's, not the application's.

    Asserted by trying to violate it directly, because an application-only rule
    would still hold in these tests while failing under any second writer.
    """
    await client.post("/v1/session", headers=auth("alice"))
    from tests.conftest import ADMIN_DSN, _swap_dbname

    dsn = _swap_dbname(ADMIN_DSN, settings.db_name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        user_id = conn.execute(
            "select id from blocks.users where subject = 'alice'"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "insert into blocks.tenants (kind, name, personal_for)"
                " values ('personal', 'second', %s)",
                (user_id,),
            )


async def test_unsigned_and_absent_tokens_are_rejected(client):
    assert (await client.post("/v1/session")).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    response = await client.post("/v1/session", headers=bad)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_token_from_an_untrusted_issuer_is_rejected(client):
    """A well-formed token signed with the wrong key must not be accepted."""
    import jwt
    import time

    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": DEV_ISSUER,
            "aud": "blocks-service-dev",
            "sub": "mallory",
            "iat": now,
            "exp": now + 600,
        },
        "not-the-configured-secret-but-long-enough",
        algorithm="HS256",
    )
    response = await client.post("/v1/session", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
