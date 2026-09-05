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


# --------------------------------------------------------------------------
# Re-entry across a closed session (architecture ruling, 2026-09-05)
# --------------------------------------------------------------------------
#
# Invariant 4: "re-entry must make the newest parking record immediately
# available". An atomic park-and-close leaves the session closed, so excluding
# closed sessions from /sessions/current would hide the one thing the invariant
# guarantees, after the most common offline boundary.


async def test_re_entry_surfaces_a_continuation_left_on_a_closed_session(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)

    closed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=CONTINUATION,
    )
    assert closed.json()["state"] == "closed"

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["id"] == session["id"]
    assert body["session"]["state"] == "closed", "the client needs to know it cannot resume"
    assert body["parking"]["next_physical_action"] == CONTINUATION["next_physical_action"]
    assert body["parking"]["id"] == closed.json()["parking_record_id"]


async def test_a_live_session_still_takes_precedence_over_a_closed_one(client, auth):
    """The fallback is a fallback: it must not shadow something resumable."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    first = await _session(client, auth, tenant, project)
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{first['id']}/close",
        headers={**auth("alice"), "If-Match": str(first["row_version"])},
        json=CONTINUATION,
    )
    second = await _session(client, auth, tenant, project)

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.json()["session"]["id"] == second["id"]
    assert r.json()["session"]["state"] == "active"


async def test_the_newest_continuation_wins_across_closed_sessions(client, auth):
    """Newer-record precedence, when both sources are closed."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    for marker in ("older continuation", "newer continuation"):
        session = await _session(client, auth, tenant, project)
        await client.post(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
            headers={**auth("alice"), "If-Match": str(session["row_version"])},
            json={**CONTINUATION, "next_physical_action": marker},
        )

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.json()["parking"]["next_physical_action"] == "newer continuation"


async def test_a_superseded_record_is_not_offered_at_re_entry(client, auth, db: Database):
    """"Newest non-superseded" — a correction replaces what re-entry shows."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]

    closed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=CONTINUATION,
    )
    original = closed.json()["parking_record_id"]

    # Append a correction. Records are immutable, so a correction is a new row
    # linked by supersedes_id — the API for that is not built yet, so this
    # exercises the query the way the data model intends.
    async with db.tenant_transaction(tenant, actor) as conn:
        await conn.execute(
            "insert into blocks.parking_records (tenant_id, work_session_id, where_i_am,"
            " next_physical_action, must_not_forget, authored_by_user_id, sequence,"
            " supersedes_id) values (%s, %s, 'corrected', 'the corrected action',"
            " 'corrected note', %s, 2, %s)",
            (tenant, session["id"], actor, original),
        )

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.json()["parking"]["next_physical_action"] == "the corrected action"


async def test_a_clean_close_leaves_nothing_to_re_enter(client, auth):
    """A session closed on a completed core has no continuation, and says so."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _session(client, auth, tenant, project)

    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json={"outcome_tier": "core"},
    )

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.json()["session"] is None


async def test_re_entry_never_crosses_a_tenant(client, auth):
    """The fallback query must inherit the same isolation as everything else."""
    alice_tenant = await _tenant(client, auth, "alice")
    project = await _project(client, auth, alice_tenant)
    session = (await _pickup(client, auth, alice_tenant, project)).json()
    await client.post(
        f"/v1/tenants/{alice_tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=CONTINUATION,
    )

    bob_tenant = await _tenant(client, auth, "bob")
    r = await client.get(f"/v1/tenants/{bob_tenant}/sessions/current", headers=auth("bob"))
    assert r.status_code == 200
    assert r.json()["session"] is None

    forbidden = await client.get(
        f"/v1/tenants/{alice_tenant}/sessions/current", headers=auth("bob")
    )
    assert forbidden.status_code == 403
