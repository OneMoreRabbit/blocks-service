"""The operating loop, and the constitutional invariants it has to hold."""

from __future__ import annotations

import psycopg
import pytest

from blocks_service.db import Database


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _project(client, auth, tenant, who="alice", name="Kitchen rewire") -> str:
    r = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who), json={"name": name}
    )
    return r.json()["id"]


async def _pickup(client, auth, tenant, project, who="alice", **kw) -> dict:
    body = {"project_id": project, "core_outcome": "Rough in the sockets"} | kw
    r = await client.post(f"/v1/tenants/{tenant}/sessions", headers=auth(who), json=body)
    return r


PARK = {
    "where_i_am": "Two sockets rough-wired, third box mounted",
    "next_physical_action": "Strip the 2.5mm cable at the third box",
    "must_not_forget": "The circuit is still live at the board",
}


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


async def test_pickup_park_resume_close(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    started = await _pickup(client, auth, tenant, project)
    assert started.status_code == 201
    session = started.json()
    assert session["state"] == "active"
    assert session["row_version"] == 1

    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=PARK,
    )
    assert parked.status_code == 200
    assert parked.json()["session"]["state"] == "parked"
    assert parked.json()["parking"]["next_physical_action"] == PARK["next_physical_action"]
    version = parked.json()["session"]["row_version"]

    resumed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/resume",
        headers={**auth("alice"), "If-Match": str(version)},
    )
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "active"

    closed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(resumed.json()["row_version"])},
        json={"outcome_tier": "core"},
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


async def test_a_session_requires_a_core_outcome(client, auth):
    """Invariant 3: a block with no core outcome cannot close anything."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=auth("alice"),
        json={"project_id": project, "core_outcome": ""},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_only_one_active_session_per_user(client, auth):
    """Picking up another block must first park or close the current one."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    assert (await _pickup(client, auth, tenant, project)).status_code == 201

    second = await _pickup(client, auth, tenant, project)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_parking_frees_the_user_to_pick_up_again(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    first = (await _pickup(client, auth, tenant, project)).json()

    await client.post(
        f"/v1/tenants/{tenant}/sessions/{first['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )
    assert (await _pickup(client, auth, tenant, project)).status_code == 201


# --------------------------------------------------------------------------
# Optimistic concurrency and the state machine
# --------------------------------------------------------------------------


async def test_park_requires_if_match(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park", headers=auth("alice"), json=PARK
    )
    assert r.status_code == 400
    assert "If-Match" in r.json()["error"]["message"]


async def test_a_stale_if_match_is_refused(client, auth):
    """The offline-client case: two devices acting on the same session."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    first = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )
    assert first.status_code == 200

    stale = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/resume",
        headers={**auth("alice"), "If-Match": "1"},
    )
    assert stale.status_code == 409


async def test_a_closed_session_cannot_be_resumed(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    closed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={"outcome_tier": "core"},
    )
    assert closed.status_code == 200

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/resume",
        headers={**auth("alice"), "If-Match": str(closed.json()["row_version"])},
    )
    assert r.status_code == 409
    assert "closed" in r.json()["error"]["message"]


# --------------------------------------------------------------------------
# The parking contract (invariant 4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["where_i_am", "next_physical_action", "must_not_forget"])
async def test_all_three_questions_are_required(client, auth, missing):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    body = {k: v for k, v in PARK.items() if k != missing}
    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=body,
    )
    assert r.status_code == 400


async def test_parking_records_are_immutable_to_the_service(client, auth, db: Database):
    """Immutability is a privilege, not a convention: blocks_app has no UPDATE.

    An application bug that tried to edit a parking record would be refused by
    PostgreSQL, which is the point of granting only SELECT and INSERT.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )

    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    async with db.tenant_transaction(tenant, actor) as conn:
        # Savepoints: the first privilege error aborts the transaction, so each
        # attempt needs its own or the second reports the abort, not the denial.
        for sql in (
            "update blocks.parking_records set where_i_am = 'rewritten'",
            "delete from blocks.parking_records",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(sql)


async def test_re_entry_returns_the_newest_record(client, auth):
    """Invariant 2: restarting after time away requires no audit."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )

    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["id"] == session["id"]
    assert body["session"]["state"] == "parked"
    assert body["parking"]["next_physical_action"] == PARK["next_physical_action"]
    assert body["parking"]["must_not_forget"] == PARK["must_not_forget"]


async def test_re_entry_is_empty_for_a_fresh_user(client, auth):
    tenant = await _tenant(client, auth)
    r = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert r.status_code == 200
    assert r.json()["session"] is None


async def test_parking_history_keeps_every_record(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    version = 1
    for n in range(2):
        parked = await client.post(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
            headers={**auth("alice"), "If-Match": str(version)},
            json={**PARK, "where_i_am": f"checkpoint {n}"},
        )
        assert parked.status_code == 200
        version = parked.json()["session"]["row_version"]
        resumed = await client.post(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/resume",
            headers={**auth("alice"), "If-Match": str(version)},
        )
        version = resumed.json()["row_version"]

    history = await client.get(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/parking", headers=auth("alice")
    )
    assert [r["where_i_am"] for r in history.json()] == ["checkpoint 0", "checkpoint 1"]
    assert [r["sequence"] for r in history.json()] == [1, 2]


# --------------------------------------------------------------------------
# Tenant isolation, again, on the new tables
# --------------------------------------------------------------------------


async def test_another_tenant_cannot_reach_these_sessions(client, auth):
    tenant = await _tenant(client, auth, "alice")
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()
    await _tenant(client, auth, "bob")

    for path, method in [
        (f"/v1/tenants/{tenant}/sessions/current", "get"),
        (f"/v1/tenants/{tenant}/sessions/{session['id']}/parking", "get"),
    ]:
        r = await getattr(client, method)(path, headers=auth("bob"))
        assert r.status_code == 403, path


# --------------------------------------------------------------------------
# Invariant 3 at the boundary, and the pickup selector — machine-enforced
# --------------------------------------------------------------------------


async def test_closing_with_neither_an_outcome_nor_a_parked_continuation_is_refused(
    client, auth
):
    """Invariant 3: a boundary preserves a completed core OR a parked continuation.

    A close carrying neither is the silent half-finished state the constitution
    rules out, so it is refused rather than accepted and quietly lost.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    r = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={},
    )
    assert r.status_code == 409
    assert "parked continuation" in r.json()["error"]["message"]

    # ...and the session is untouched, so the client can still park or state a tier.
    current = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert current.json()["session"]["state"] == "active"


async def test_closing_without_a_tier_is_allowed_after_parking(client, auth):
    """The other half of the rule: an explicitly parked continuation qualifies."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()

    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )
    version = parked.json()["session"]["row_version"]

    closed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(version)},
        json={},
    )
    assert closed.status_code == 200


@pytest.mark.parametrize(
    "body, why",
    [
        ({"core_outcome": "x"}, "neither selector"),
        ({"core_outcome": "x", "project_id": None, "tag_token": None}, "both null"),
    ],
)
async def test_pickup_requires_exactly_one_selector(client, auth, body, why):
    tenant = await _tenant(client, auth)
    r = await client.post(f"/v1/tenants/{tenant}/sessions", headers=auth("alice"), json=body)
    assert r.status_code == 400, why
    assert r.json()["error"]["code"] == "invalid_request"


async def test_pickup_rejects_both_selectors_together(client, auth):
    """A tag resolves to a project, so sending both invites a silent disagreement."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    registered = await client.post(
        f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={"label": "Block 7"}
    )
    r = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=auth("alice"),
        json={
            "project_id": project,
            "tag_token": registered.json()["tag_token"],
            "core_outcome": "x",
        },
    )
    assert r.status_code == 400
