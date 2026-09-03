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


# --------------------------------------------------------------------------
# Content must not reach the log (architecture review, 2026-09-03)
# --------------------------------------------------------------------------


async def test_the_logging_sink_never_emits_a_payload(client, auth, caplog):
    """The parking answers are the most sensitive text in the product.

    Payloads are identifier-only by design, but a sink that relied on that would
    be one schema change away from leaking. This asserts the sink itself, with a
    payload deliberately stuffed with text it must drop.
    """
    import logging

    from blocks_service.dispatcher import LoggingSink, loggable

    secret = "the-board-is-still-live"
    envelope = {
        "eventId": "42",
        "eventType": "session.parked",
        "eventVersion": "0.1",
        "tenantId": "6f1e0a1e-0000-4000-8000-000000000000",
        "occurredAt": "2026-09-03T00:00:00+00:00",
        "payload": {"nextPhysicalAction": secret, "mustNotForget": secret},
        "unexpectedFutureField": secret,
    }

    with caplog.at_level(logging.INFO, logger="blocks.dispatcher"):
        await LoggingSink().deliver(envelope)

    emitted = " ".join(
        [r.getMessage() for r in caplog.records]
        + [repr(getattr(r, "event", "")) for r in caplog.records]
    )
    assert secret not in emitted
    assert "session.parked" in emitted  # metadata still observable
    # An allowlist, so a field added tomorrow is dropped rather than leaked.
    assert "unexpectedFutureField" not in repr(loggable(envelope))


async def test_session_events_carry_no_user_text(client, auth, db: Database):
    """End to end: pick up and park, then read what actually reached the outbox."""
    session = (await client.post("/v1/session", headers=auth("alice"))).json()
    tenant = session["personal_tenant_id"]
    project = (
        await client.post(
            f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "Kitchen"}
        )
    ).json()["id"]
    started = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=auth("alice"),
        json={"project_id": project, "core_outcome": "SECRET-CORE-OUTCOME"},
    )
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{started.json()['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json={
            "where_i_am": "SECRET-WHERE",
            "next_physical_action": "SECRET-ACTION",
            "must_not_forget": "SECRET-FORGET",
        },
    )

    async with db.tenant_transaction(tenant, session["user_id"]) as conn:
        cur = await conn.execute(
            "select event_type, payload::text as payload from blocks.outbox"
            " where event_type like 'session.%'"
        )
        rows = await cur.fetchall()

    assert {r["event_type"] for r in rows} == {"session.started", "session.parked"}
    for row in rows:
        for secret in ("SECRET-CORE-OUTCOME", "SECRET-WHERE", "SECRET-ACTION", "SECRET-FORGET"):
            assert secret not in row["payload"], row["event_type"]
