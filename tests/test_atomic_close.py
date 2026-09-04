"""Atomic "preserve this continuation and close".

Android's offline boundary, from their 0.2 consumer review: a client that queues
park-then-close while offline cannot supply a valid `If-Match` on the close. The
park advances the row version, and the queued close was written before that
version existed — sending the pre-park version is stale, omitting it is invalid,
and replaying close first fails because no continuation exists yet. FIFO ordering
does not help; the dependency is on a value that did not exist when the intent
was recorded.

So it is one operation, under the one version the client actually knows.
"""

from __future__ import annotations

import pytest

from blocks_service.db import Database
from tests.test_operating_loop import _pickup, _project, _tenant

CONTINUATION = {
    "where_i_am": "Two sockets wired, third box mounted",
    "next_physical_action": "Strip the 2.5mm cable at the third box",
    "must_not_forget": "The circuit is still live at the board",
}


async def _session(client, auth, tenant, project) -> dict:
    return (await _pickup(client, auth, tenant, project)).json()


async def test_first_execution_parks_and_closes_in_one_call(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=CONTINUATION,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "closed"
    assert body["parking_record_id"] is not None
    assert body["row_version"] > session["row_version"]

    history = await client.get(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/parking", headers=auth("alice")
    )
    assert [p["next_physical_action"] for p in history.json()] == [
        CONTINUATION["next_physical_action"]
    ]


async def test_the_version_used_is_the_one_the_client_already_held(client, auth):
    """The whole point: no second round trip to learn a post-park version."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    assert session["row_version"] == 1

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json=CONTINUATION,
    )
    assert r.status_code == 200


async def test_idempotent_replay_returns_the_stored_result_unchanged(client, auth):
    """Replay must succeed despite the row version having advanced.

    A retried queue item re-sends the ORIGINAL If-Match, which is now stale. It
    must not be re-executed and must not be refused: the stored outcome is
    replayed byte for byte.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    headers = {
        **auth("alice"),
        "If-Match": str(session["row_version"]),
        "Idempotency-Key": "queued-close-1",
    }

    first = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers=headers, json=CONTINUATION,
    )
    second = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers=headers, json=CONTINUATION,
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers.get("Idempotent-Replay") == "true"

    # Exactly one parking record, not two.
    history = await client.get(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/parking", headers=auth("alice")
    )
    assert len(history.json()) == 1


async def test_the_same_key_with_a_different_body_is_refused(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    headers = {
        **auth("alice"),
        "If-Match": str(session["row_version"]),
        "Idempotency-Key": "queued-close-1",
    }

    assert (
        await client.post(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
            headers=headers, json=CONTINUATION,
        )
    ).status_code == 200

    clash = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers=headers,
        json={**CONTINUATION, "must_not_forget": "something else entirely"},
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"


async def test_a_stale_version_is_refused_and_writes_nothing(client, auth, db: Database):
    """Transactional rollback: not even the parking record survives.

    This is the property that makes the atomic form safe to queue. A partial
    application — record written, session left open — would be worse than either
    outcome, because the next replay would append a second record.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]

    # Something else advances the session first.
    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=CONTINUATION,
    )
    assert parked.status_code == 200

    stale = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={**CONTINUATION, "where_i_am": "a second, rolled-back record"},
    )
    assert stale.status_code == 409

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select where_i_am, state::text as state from blocks.parking_records p"
            " join blocks.work_sessions w on w.id = p.work_session_id"
            " where p.work_session_id = %s",
            (session["id"],),
        )
        rows = await cur.fetchall()

    assert len(rows) == 1, "the rolled-back close must not have appended a record"
    assert rows[0]["where_i_am"] == CONTINUATION["where_i_am"]
    assert rows[0]["state"] == "parked", "the session must be untouched"


async def test_both_forms_together_are_refused(client, auth):
    """A closed union: a body carrying both matches neither member."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={**CONTINUATION, "outcome_tier": "core"},
    )
    assert r.status_code == 400


async def test_the_events_carry_no_parking_text(client, auth, db: Database):
    """Both facts are announced; neither payload carries the three answers."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]

    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json=CONTINUATION,
    )

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select event_type, payload::text as payload from blocks.outbox"
            " where event_type like 'session.%' order by id"
        )
        rows = await cur.fetchall()

    assert [r["event_type"] for r in rows] == [
        "session.started", "session.parked", "session.closed",
    ]
    for row in rows:
        for answer in CONTINUATION.values():
            assert answer not in row["payload"], row["event_type"]


async def test_calibration_is_still_recorded_for_an_atomic_close(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)

    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={**CONTINUATION, "interruption": "hijacked"},
    )

    [event] = (
        await client.get(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
        )
    ).json()
    assert event["interruption"] == "hijacked"
    assert event["actual_tier"] is None
