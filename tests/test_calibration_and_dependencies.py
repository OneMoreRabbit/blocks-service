"""Calibration evidence (invariant 8) and map dependencies (invariant 1)."""

from __future__ import annotations

import psycopg
import pytest

from blocks_service.db import Database
from tests.test_operating_loop import PARK, _pickup, _project, _tenant


async def _closed_session(client, auth, tenant, project, **close) -> dict:
    session = (await _pickup(client, auth, tenant, project)).json()
    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": str(session["row_version"])},
        json=PARK,
    )
    version = parked.json()["session"]["row_version"]
    resumed = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/resume",
        headers={**auth("alice"), "If-Match": str(version)},
    )
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(resumed.json()["row_version"])},
        json=close,
    )
    return session


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


async def test_closing_records_what_the_session_measured(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _closed_session(
        client, auth, tenant, project, outcome_tier="core", interruption="boundary"
    )

    r = await client.get(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
    )
    assert r.status_code == 200
    [event] = r.json()
    assert event["actual_tier"] == "core"
    assert event["interruption"] == "boundary"
    assert event["elapsed_seconds"] >= 0
    assert event["restart_latency_seconds"] is not None


async def test_planned_tier_is_the_highest_the_session_committed_to(client, auth):
    """"Planned versus actual" is meaningless unless planned is what was committed."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (
        await _pickup(
            client, auth, tenant, project,
            full_outcome="Second socket too", polish_outcome="Tidy the runs",
        )
    ).json()
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": "1"},
        json={"outcome_tier": "core"},
    )

    r = await client.get(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
    )
    [event] = r.json()
    assert event["planned_tier"] == "polish"
    assert event["actual_tier"] == "core"


async def test_a_session_closed_without_a_tier_still_records_evidence(client, auth):
    """Forcing a value would manufacture evidence; absence is a real observation.

    Parked first, because invariant 3 requires a close to preserve either a
    completed core or an explicitly parked continuation — this is the second case.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()
    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(parked.json()["session"]["row_version"])},
        json={},
    )

    [event] = (
        await client.get(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
        )
    ).json()
    assert event["actual_tier"] is None
    assert event["planned_tier"] == "core"


async def test_a_hijacked_block_is_recorded_neutrally(client, auth):
    """Invariant 2, in full: "a hijacked block closes early, parks its state, and
    rolls forward" — so the park is part of the behaviour, not a test detail."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = (await _pickup(client, auth, tenant, project)).json()
    parked = await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/park",
        headers={**auth("alice"), "If-Match": "1"},
        json=PARK,
    )
    await client.post(
        f"/v1/tenants/{tenant}/sessions/{session['id']}/close",
        headers={**auth("alice"), "If-Match": str(parked.json()["session"]["row_version"])},
        json={"interruption": "hijacked"},
    )

    [event] = (
        await client.get(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
        )
    ).json()
    assert event["interruption"] == "hijacked"


async def test_calibration_is_append_only(client, auth, db: Database):
    """Evidence that can be edited after the fact is not evidence."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    await _closed_session(client, auth, tenant, project, outcome_tier="core")
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]

    async with db.tenant_transaction(tenant, actor) as conn:
        for sql in (
            "update blocks.calibration_events set actual_tier = 'polish'",
            "delete from blocks.calibration_events",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(sql)


async def test_no_endpoint_returns_a_score_or_a_streak(client, auth):
    """Invariant 8 is honoured by having nothing to present, not by withholding."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    session = await _closed_session(client, auth, tenant, project, outcome_tier="core")

    body = (
        await client.get(
            f"/v1/tenants/{tenant}/sessions/{session['id']}/calibration", headers=auth("alice")
        )
    ).text.lower()
    for word in ("streak", "score", "rate", "percent", "total", "rank"):
        assert word not in body


# --------------------------------------------------------------------------
# Map dependencies
# --------------------------------------------------------------------------


async def _map_item(client, auth, tenant, project, title) -> str:
    r = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/map",
        headers=auth("alice"),
        json={"title": title},
    )
    return r.json()["id"]


async def test_a_dependency_can_be_declared_and_read(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    first = await _map_item(client, auth, tenant, project, "First fix")
    second = await _map_item(client, auth, tenant, project, "Second fix")

    r = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/map/dependencies",
        headers=auth("alice"),
        json={"predecessor_id": first, "successor_id": second},
    )
    assert r.status_code == 201

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project}/map/dependencies", headers=auth("alice")
    )
    assert listed.json() == [{"predecessor_id": first, "successor_id": second}]


async def test_a_self_dependency_is_refused(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    item = await _map_item(client, auth, tenant, project, "First fix")

    r = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/map/dependencies",
        headers=auth("alice"),
        json={"predecessor_id": item, "successor_id": item},
    )
    assert r.status_code == 409


async def test_a_cycle_is_refused(client, auth):
    """A cyclic map can never be ordered, and being orderable is its whole job."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    a = await _map_item(client, auth, tenant, project, "A")
    b = await _map_item(client, auth, tenant, project, "B")
    c = await _map_item(client, auth, tenant, project, "C")

    for pred, succ in ((a, b), (b, c)):
        r = await client.post(
            f"/v1/tenants/{tenant}/projects/{project}/map/dependencies",
            headers=auth("alice"),
            json={"predecessor_id": pred, "successor_id": succ},
        )
        assert r.status_code == 201

    closing = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/map/dependencies",
        headers=auth("alice"),
        json={"predecessor_id": c, "successor_id": a},
    )
    assert closing.status_code == 409
    assert "cycle" in closing.json()["error"]["message"]


async def test_a_duplicate_dependency_is_refused(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    a = await _map_item(client, auth, tenant, project, "A")
    b = await _map_item(client, auth, tenant, project, "B")
    body = {"predecessor_id": a, "successor_id": b}
    path = f"/v1/tenants/{tenant}/projects/{project}/map/dependencies"

    assert (await client.post(path, headers=auth("alice"), json=body)).status_code == 201
    assert (await client.post(path, headers=auth("alice"), json=body)).status_code == 409


async def test_a_dependency_can_be_removed(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    a = await _map_item(client, auth, tenant, project, "A")
    b = await _map_item(client, auth, tenant, project, "B")
    path = f"/v1/tenants/{tenant}/projects/{project}/map/dependencies"
    await client.post(path, headers=auth("alice"), json={"predecessor_id": a, "successor_id": b})

    gone = await client.delete(
        f"{path}?predecessor_id={a}&successor_id={b}", headers=auth("alice")
    )
    assert gone.status_code == 204
    assert (await client.get(path, headers=auth("alice"))).json() == []


async def test_items_from_another_project_cannot_be_linked(client, auth):
    tenant = await _tenant(client, auth)
    one = await _project(client, auth, tenant, name="Kitchen")
    two = await _project(client, auth, tenant, name="Bathroom")
    a = await _map_item(client, auth, tenant, one, "A")
    b = await _map_item(client, auth, tenant, two, "B")

    r = await client.post(
        f"/v1/tenants/{tenant}/projects/{one}/map/dependencies",
        headers=auth("alice"),
        json={"predecessor_id": a, "successor_id": b},
    )
    assert r.status_code == 404
