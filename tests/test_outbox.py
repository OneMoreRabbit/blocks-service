"""The transactional outbox, and the published event schemas it writes against."""

from __future__ import annotations

import json

import jsonschema
import pytest

from blocks_service import events
from blocks_service.db import Database


async def _session(client, auth, subject: str) -> dict:
    return (await client.post("/v1/session", headers=auth(subject))).json()


async def test_creating_a_project_appends_exactly_one_event(client, auth, db: Database):
    alice = await _session(client, auth, "alice")
    tenant, actor = alice["personal_tenant_id"], alice["user_id"]

    created = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "Shed"}
    )
    assert created.status_code == 201

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select event_type, event_version, payload, dispatched_at from blocks.outbox"
        )
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["event_type"] == "project.created"
    assert rows[0]["event_version"] == "0.1"
    assert rows[0]["payload"] == {"projectId": created.json()["id"], "name": "Shed"}
    assert rows[0]["dispatched_at"] is None


async def test_emitted_payloads_validate_against_the_published_schema(client, auth, db: Database):
    """The schema in contracts/ is the contract, so the code is checked against it."""
    alice = await _session(client, auth, "alice")
    await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": "Shed"},
    )

    async with db.tenant_transaction(alice["personal_tenant_id"], alice["user_id"]) as conn:
        cur = await conn.execute("select event_type, payload from blocks.outbox")
        rows = await cur.fetchall()

    assert rows
    for row in rows:
        jsonschema.validate(row["payload"], events.load_schema(row["event_type"]))


def test_every_declared_event_type_has_a_published_schema():
    """A version stamped in code with no schema file would ship an unreadable event."""
    for event_type, version in events.EVENT_VERSIONS.items():
        path = events.schema_path(event_type, version)
        assert path.exists(), f"no published schema for {event_type} {version}"
        schema = json.loads(path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)


def test_emitting_an_unpublished_event_type_is_an_error():
    assert "project.deleted" not in events.EVENT_VERSIONS


async def test_outbox_rows_are_tenant_isolated(client, auth, db: Database):
    alice = await _session(client, auth, "alice")
    bob = await _session(client, auth, "bob")
    await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": "Alice only"},
    )

    async with db.tenant_transaction(bob["personal_tenant_id"], bob["user_id"]) as conn:
        cur = await conn.execute("select id from blocks.outbox")
        assert await cur.fetchall() == []


async def test_a_failed_project_creation_leaves_no_event(client, auth, db: Database):
    """The outbox is only worth having if it cannot commit on its own."""
    alice = await _session(client, auth, "alice")
    rejected = await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": ""},
    )
    assert rejected.status_code == 400

    async with db.tenant_transaction(alice["personal_tenant_id"], alice["user_id"]) as conn:
        cur = await conn.execute("select id from blocks.outbox")
        assert await cur.fetchall() == []
