"""Local-first convergence (ADR-0011): client identity, replay, authored order.

The premise is that a client may be offline for an unbounded time, so nothing may
depend on the server still *remembering* an operation. Identity has to carry it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blocks_service.db import Database

PARK = {
    "where_i_am": "Two sockets wired",
    "next_physical_action": "Strip the cable at the third box",
    "must_not_forget": "The board is live",
}


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _expire_results(db: Database, tenant: str, actor: str) -> int:
    """Simulate the ordinary idempotency-result record aging out.

    Retention is declared as a minimum, not a maximum, so a client that returns
    after longer than the window finds the result gone. That is the case resource
    identity exists for, and the only way to test it is to delete the record.
    """
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("delete from blocks.idempotency_keys")
        return cur.rowcount


# --------------------------------------------------------------------------
# Client-generated identity: replay before and after result expiry
# --------------------------------------------------------------------------


async def test_create_with_a_client_id_replays_from_the_stored_result(client, auth):
    tenant = await _tenant(client, auth)
    pid, key = str(uuid.uuid4()), "op-" + uuid.uuid4().hex[:8]
    body = {"client_id": pid, "name": "Kitchen rewire"}
    headers = {**auth("alice"), "Idempotency-Key": key}

    first = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)
    second = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)

    assert first.status_code == 201
    assert first.json()["client_id"] == pid, "the client's own identifier comes back"
    assert first.json()["id"] != pid, "the canonical id stays server-generated"
    assert second.status_code == 201, "a retained result replays its original response"
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.headers.get("Idempotent-Replay-Source") is None


async def test_create_replays_by_resource_identity_after_the_result_expires(
    client, auth, db: Database
):
    """The case the whole design exists for: offline longer than the window."""
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    pid, key = str(uuid.uuid4()), "op-" + uuid.uuid4().hex[:8]
    body = {"client_id": pid, "name": "Kitchen rewire"}
    headers = {**auth("alice"), "Idempotency-Key": key}

    first = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)
    assert first.status_code == 201
    assert await _expire_results(db, tenant, actor) >= 1

    replay = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)
    assert replay.status_code == 200, "nothing was created, so not 201"
    assert replay.json()["client_id"] == pid
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.headers.get("Idempotent-Replay-Source") == "resource"

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert [p["client_id"] for p in listed.json()] == [pid], "exactly one project, no duplicate"


async def test_reusing_a_client_id_for_a_different_operation_conflicts(
    client, auth, db: Database
):
    """First creation wins; never last-write-wins."""
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    pid = str(uuid.uuid4())

    original = await client.post(
        f"/v1/tenants/{tenant}/projects",
        headers={**auth("alice"), "Idempotency-Key": "op-a"},
        json={"client_id": pid, "name": "Kitchen rewire"},
    )
    assert original.status_code == 201
    await _expire_results(db, tenant, actor)

    for label, key, name in [
        ("different body", "op-a", "Loft conversion"),
        ("different key", "op-b", "Kitchen rewire"),
    ]:
        clash = await client.post(
            f"/v1/tenants/{tenant}/projects",
            headers={**auth("alice"), "Idempotency-Key": key},
            json={"client_id": pid, "name": name},
        )
        assert clash.status_code == 409, label
        assert clash.json()["error"]["code"] == "conflict", label

    canonical = original.json()["id"]
    page = await client.get(f"/v1/tenants/{tenant}/projects/{canonical}", headers=auth("alice"))
    assert page.json()["project"]["name"] == "Kitchen rewire", "first creation wins"


async def test_a_later_rename_is_a_separate_operation_not_a_create_conflict(client, auth):
    """A name edited after Create is an ordered rename; the queued Create is frozen."""
    tenant = await _tenant(client, auth)
    pid = str(uuid.uuid4())
    created = await client.post(
        f"/v1/tenants/{tenant}/projects",
        headers={**auth("alice"), "Idempotency-Key": "op-a"},
        json={"client_id": pid, "name": "Kitchen rewire"},
    )
    renamed = await client.patch(
        f"/v1/tenants/{tenant}/projects/{created.json()['id']}",
        headers={**auth("alice"), "If-Match": str(created.json()["row_version"])},
        json={"name": "Kitchen rewire v2"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Kitchen rewire v2"


async def test_a_client_id_confers_no_ownership(client, auth):
    """Identity is chosen by the client; ownership never is."""
    alice_tenant = await _tenant(client, auth, "alice")
    bob_tenant = await _tenant(client, auth, "bob")
    pid = str(uuid.uuid4())

    alice = await client.post(
        f"/v1/tenants/{alice_tenant}/projects",
        headers={**auth("alice"), "Idempotency-Key": "op-a"},
        json={"client_id": pid, "name": "Alice's"},
    )
    # The SAME client_id in Bob's tenant must succeed as an independent resource —
    # not conflict, not replay, and not reveal that Alice used the value.
    bob = await client.post(
        f"/v1/tenants/{bob_tenant}/projects",
        headers={**auth("bob"), "Idempotency-Key": "op-a"},
        json={"client_id": pid, "name": "Bob's"},
    )
    assert alice.status_code == 201 and bob.status_code == 201, "no oracle: both create"
    assert alice.json()["id"] != bob.json()["id"], "distinct canonical ids"
    assert alice.json()["client_id"] == bob.json()["client_id"] == pid

    for tenant, who, expected in [(alice_tenant, "alice", "Alice's"), (bob_tenant, "bob", "Bob's")]:
        page = await client.get(
            f"/v1/tenants/{tenant}/projects/"
            f"{(alice if who == 'alice' else bob).json()['id']}",
            headers=auth(who),
        )
        assert page.json()["project"]["name"] == expected


async def test_park_append_replays_by_identity_after_expiry(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    rid, key = str(uuid.uuid4()), "park-op"
    body = {**PARK, "client_id": rid}
    headers = {**auth("alice"), "Idempotency-Key": key}
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"

    first = await client.post(path, headers=headers, json=body)
    assert first.status_code == 201 and first.json()["client_id"] == rid
    await _expire_results(db, tenant, actor)

    replay = await client.post(path, headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay-Source") == "resource"
    assert len((await client.get(path, headers=auth("alice"))).json()) == 1


# --------------------------------------------------------------------------
# 404 absent vs 409 stale, under forced tenant RLS
# --------------------------------------------------------------------------


async def test_absent_project_is_404_and_stale_version_is_409(client, auth):
    """A client mid-remap must tell "wrong identity" from "stale version"."""
    tenant = await _tenant(client, auth)
    real = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    ghost = str(uuid.uuid4())

    cases = [
        ("notes absent", "put", f"/projects/{ghost}/notes", "1", {"body": "x"}, 404),
        ("notes stale", "put", f"/projects/{real}/notes", "99", {"body": "x"}, 409),
        ("rename absent", "patch", f"/projects/{ghost}", "1", {"name": "n"}, 404),
        ("rename stale", "patch", f"/projects/{real}", "99", {"name": "n"}, 409),
        ("archive absent", "post", f"/projects/{ghost}/archive", "1", None, 404),
    ]
    for label, method, suffix, version, body, expected in cases:
        kwargs = {"headers": {**auth("alice"), "If-Match": version}}
        if body is not None:
            kwargs["json"] = body
        r = await getattr(client, method)(f"/v1/tenants/{tenant}{suffix}", **kwargs)
        assert r.status_code == expected, f"{label}: got {r.status_code}"


async def test_another_tenants_project_is_404_not_a_leak(client, auth):
    """Absent and unauthorized must be indistinguishable from outside."""
    alice_tenant = await _tenant(client, auth, "alice")
    real = (await client.post(
        f"/v1/tenants/{alice_tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    bob_tenant = await _tenant(client, auth, "bob")
    ghost = str(uuid.uuid4())

    for target in (real, ghost):
        r = await client.put(
            f"/v1/tenants/{bob_tenant}/projects/{target}/notes",
            headers={**auth("bob"), "If-Match": "1"}, json={"body": "x"},
        )
        assert r.status_code == 404, "a real id from another tenant must look absent"


# --------------------------------------------------------------------------
# Authored ordering
# --------------------------------------------------------------------------


async def test_parking_orders_by_authored_time_not_arrival(client, auth):
    """Delayed delivery must not rewrite the user's own history."""
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"
    base = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    # Delivered newest-authored FIRST, so arrival order is the reverse of authorship.
    for label, minutes in [("written 09:20", 20), ("written 09:00", 0), ("written 09:10", 10)]:
        r = await client.post(
            path, headers=auth("alice"),
            json={**PARK, "where_i_am": label,
                  "occurred_at": (base + timedelta(minutes=minutes)).isoformat()},
        )
        assert r.status_code == 201

    history = await client.get(path, headers=auth("alice"))
    assert [p["where_i_am"] for p in history.json()] == [
        "written 09:20", "written 09:10", "written 09:00",
    ]
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}", headers=auth("alice"))
    assert page.json()["newest_parking"]["where_i_am"] == "written 09:20"


async def test_ties_are_broken_deterministically(client, auth):
    """Two parks authored in the same instant must not flicker between queries."""
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"
    same = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc).isoformat()

    for n in range(4):
        await client.post(
            path, headers=auth("alice"),
            json={**PARK, "where_i_am": f"tie {n}", "occurred_at": same},
        )
    orders = [
        [p["id"] for p in (await client.get(path, headers=auth("alice"))).json()]
        for _ in range(3)
    ]
    assert orders[0] == orders[1] == orders[2], "ordering must be total, not usually-stable"


async def test_an_older_client_omitting_occurred_at_gets_acceptance_time(client, auth):
    """Runtime-supported older clients keep working and are ordered sensibly."""
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"

    r = await client.post(path, headers=auth("alice"), json=PARK)
    assert r.status_code == 201
    assert r.json()["occurred_at"] is not None, "occurred_at is required in 0.4 output"
    assert abs(
        datetime.fromisoformat(r.json()["occurred_at"])
        - datetime.fromisoformat(r.json()["created_at"])
    ) < timedelta(seconds=2), "omitted occurred_at falls back to acceptance time"


async def test_migrated_and_legacy_rows_order_with_new_ones(client, auth, db: Database):
    """Rows backfilled by 0009 carry acceptance time and must sort sensibly."""
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"

    # A row shaped like one migrated from before 0009: occurred_at == created_at.
    async with db.tenant_transaction(tenant, actor) as conn:
        await conn.execute(
            "insert into blocks.parking_records (tenant_id, project_id, where_i_am,"
            " next_physical_action, must_not_forget, authored_by_user_id, sequence,"
            " occurred_at, created_at)"
            " values (%s,%s,'legacy row','a','b',%s,1,"
            "         timestamptz '2026-01-01 09:00+00', timestamptz '2026-01-01 09:00+00')",
            (tenant, project, actor),
        )
    await client.post(
        path, headers=auth("alice"),
        json={**PARK, "where_i_am": "new row",
              "occurred_at": datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc).isoformat()},
    )
    history = await client.get(path, headers=auth("alice"))
    assert [p["where_i_am"] for p in history.json()] == ["new row", "legacy row"]


# --------------------------------------------------------------------------
# The whole offline sequence, converging once each
# --------------------------------------------------------------------------


async def test_the_offline_sequence_converges_once_each(client, auth, db: Database):
    """Create, read the canonical page, save notes, park — replayed throughout.

    Every operation is sent twice with its frozen identity, and the result store is
    expired in the middle, so both replay paths are exercised in one sequence.
    """
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    pid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    authored = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc).isoformat()

    create = {"client_id": pid, "name": "Kitchen rewire"}
    ck = {**auth("alice"), "Idempotency-Key": "create-op"}
    first = await client.post(f"/v1/tenants/{tenant}/projects", headers=ck, json=create)
    assert first.status_code == 201
    assert (await client.post(f"/v1/tenants/{tenant}/projects", headers=ck, json=create)).status_code == 201
    canonical = first.json()["id"]

    # The client reads the canonical page for the notes version. It never assumes.
    page = await client.get(f"/v1/tenants/{tenant}/projects/{canonical}", headers=auth("alice"))
    notes_version = page.json()["notes"]["row_version"]

    saved = await client.put(
        f"/v1/tenants/{tenant}/projects/{canonical}/notes",
        headers={**auth("alice"), "If-Match": str(notes_version)},
        json={"body": "wire colours"},
    )
    assert saved.status_code == 200

    await _expire_results(db, tenant, actor)

    park = {**PARK, "client_id": rid, "occurred_at": authored}
    pk = {**auth("alice"), "Idempotency-Key": "park-op"}
    path = f"/v1/tenants/{tenant}/projects/{canonical}/parking"
    assert (await client.post(path, headers=pk, json=park)).status_code == 201
    # Still inside the window this park just opened, so it replays the stored result.
    assert (await client.post(path, headers=pk, json=park)).status_code == 201

    # Expire again: now BOTH replay by resource identity instead.
    await _expire_results(db, tenant, actor)
    assert (await client.post(path, headers=pk, json=park)).status_code == 200
    assert (await client.post(f"/v1/tenants/{tenant}/projects", headers=ck, json=create)).status_code == 200

    projects = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert [p["client_id"] for p in projects.json()] == [pid]
    assert len((await client.get(path, headers=auth("alice"))).json()) == 1
    final = await client.get(f"/v1/tenants/{tenant}/projects/{canonical}", headers=auth("alice"))
    assert final.json()["notes"]["body"] == "wire colours"
    assert final.json()["newest_parking"]["occurred_at"].startswith("2026-09-08T09:00")


async def test_the_same_client_id_in_two_tenants_discloses_nothing(client, auth):
    """The oracle this design exists to remove.

    An earlier draft let the client supply the canonical id. Because that column
    is globally unique, a value another tenant already held failed the insert
    while an unused one succeeded — and the *status code* disclosed which, which no
    wording of the message could fix. Tenant-scoped identity removes the question:
    both tenants create successfully and neither observes the other.
    """
    alice_tenant = await _tenant(client, auth, "alice")
    bob_tenant = await _tenant(client, auth, "bob")
    shared = str(uuid.uuid4())

    responses = []
    for tenant, who in [(alice_tenant, "alice"), (bob_tenant, "bob")]:
        responses.append(await client.post(
            f"/v1/tenants/{tenant}/projects",
            headers={**auth(who), "Idempotency-Key": "same-key"},
            json={"client_id": shared, "name": f"{who}'s project"},
        ))
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["id"] != responses[1].json()["id"]

    # A control: a client_id nobody has used behaves identically to one another
    # tenant is using. Indistinguishable responses mean there is nothing to probe.
    control = await client.post(
        f"/v1/tenants/{bob_tenant}/projects",
        headers={**auth("bob"), "Idempotency-Key": "control"},
        json={"client_id": str(uuid.uuid4()), "name": "control"},
    )
    assert control.status_code == responses[1].status_code


async def test_park_client_ids_are_also_tenant_scoped(client, auth):
    alice_tenant = await _tenant(client, auth, "alice")
    bob_tenant = await _tenant(client, auth, "bob")
    shared = str(uuid.uuid4())
    codes = []
    for tenant, who in [(alice_tenant, "alice"), (bob_tenant, "bob")]:
        project = (await client.post(
            f"/v1/tenants/{tenant}/projects", headers=auth(who), json={"name": "P"}
        )).json()["id"]
        r = await client.post(
            f"/v1/tenants/{tenant}/projects/{project}/parking",
            headers=auth(who), json={**PARK, "client_id": shared},
        )
        codes.append(r.status_code)
    assert codes == [201, 201]


async def test_a_create_without_a_client_id_stores_no_creation_identity(client, auth, db: Database):
    """0.3 behaviour is unchanged, and the schema constraint stays satisfiable.

    A fingerprint without a client identity is a row that can never replay and
    never conflict — a state with no behaviour, which 0010 makes unreachable.
    """
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    created = await client.post(
        f"/v1/tenants/{tenant}/projects",
        headers={**auth("alice"), "Idempotency-Key": "no-client-id"},
        json={"name": "server-assigned"},
    )
    assert created.status_code == 201
    assert created.json()["client_id"] is None

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select client_id, creation_fingerprint from blocks.projects where id = %s",
            (created.json()["id"],),
        )
        row = await cur.fetchone()
    assert row["client_id"] is None and row["creation_fingerprint"] is None


# --------------------------------------------------------------------------
# Real concurrency: simultaneous delivery of the same queued operation
# --------------------------------------------------------------------------
#
# A retry that races its own original must converge, not conflict. Sequential
# tests cannot see this: both requests have to be in flight at once, each on its
# own pooled connection, for the lookup-then-insert window to open at all.


async def _gather(client, path, headers_bodies):
    import asyncio

    return await asyncio.gather(
        *(client.post(path, headers=h, json=b) for h, b in headers_bodies)
    )


async def test_identical_simultaneous_creates_converge_to_one_row(client, auth):
    tenant = await _tenant(client, auth)
    cid, key = str(uuid.uuid4()), "race-" + uuid.uuid4().hex[:6]
    headers = {**auth("alice"), "Idempotency-Key": key}
    body = {"client_id": cid, "name": "Kitchen rewire"}

    responses = await _gather(
        client, f"/v1/tenants/{tenant}/projects", [(headers, body), (headers, body)]
    )

    codes = sorted(r.status_code for r in responses)
    assert 409 not in codes, "a retry racing itself must never conflict with itself"
    assert codes in ([200, 201], [201, 201]), codes
    assert {r.json()["id"] for r in responses} == {responses[0].json()["id"]}, "one identity"

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert len(listed.json()) == 1, "exactly one row"


async def test_simultaneous_same_key_different_body_yields_one_winner(client, auth):
    tenant = await _tenant(client, auth)
    key = "race-" + uuid.uuid4().hex[:6]
    headers = {**auth("alice"), "Idempotency-Key": key}

    responses = await _gather(client, f"/v1/tenants/{tenant}/projects", [
        (headers, {"client_id": str(uuid.uuid4()), "name": "one"}),
        (headers, {"client_id": str(uuid.uuid4()), "name": "two"}),
    ])
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409], codes

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert len(listed.json()) == 1, "one winner, not two rows"


async def test_simultaneous_same_client_id_different_keys_yields_one_winner(client, auth):
    tenant = await _tenant(client, auth)
    cid = str(uuid.uuid4())

    responses = await _gather(client, f"/v1/tenants/{tenant}/projects", [
        ({**auth("alice"), "Idempotency-Key": "key-a"}, {"client_id": cid, "name": "one"}),
        ({**auth("alice"), "Idempotency-Key": "key-b"}, {"client_id": cid, "name": "two"}),
    ])
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409], codes

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert len(listed.json()) == 1


async def test_identical_simultaneous_parks_converge_to_one_record(client, auth):
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"
    cid = str(uuid.uuid4())
    headers = {**auth("alice"), "Idempotency-Key": "park-race"}
    body = {**PARK, "client_id": cid}

    responses = await _gather(client, path, [(headers, body), (headers, body)])
    assert 409 not in [r.status_code for r in responses]
    assert len((await client.get(path, headers=auth("alice"))).json()) == 1


async def test_the_service_still_works_after_every_race(client, auth):
    """No transaction left aborted and no partial idempotency state behind."""
    tenant = await _tenant(client, auth)
    key = "race-" + uuid.uuid4().hex[:6]
    await _gather(client, f"/v1/tenants/{tenant}/projects", [
        ({**auth("alice"), "Idempotency-Key": key}, {"client_id": str(uuid.uuid4()), "name": "a"}),
        ({**auth("alice"), "Idempotency-Key": key}, {"client_id": str(uuid.uuid4()), "name": "b"}),
    ])
    after = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "after"}
    )
    assert after.status_code == 201, "the service must be usable after a lost race"


async def test_superseding_the_same_record_twice_is_not_a_client_id_error(client, auth):
    """Two unique constraints, two different repairs. Do not confuse them."""
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "P"}
    )).json()["id"]
    path = f"/v1/tenants/{tenant}/projects/{project}/parking"

    original = (await client.post(path, headers=auth("alice"), json=PARK)).json()
    first = await client.post(
        path, headers=auth("alice"),
        json={**PARK, "where_i_am": "correction", "supersedes_id": original["id"]},
    )
    assert first.status_code == 201

    second = await client.post(
        path, headers=auth("alice"),
        json={**PARK, "where_i_am": "second correction", "supersedes_id": original["id"]},
    )
    assert second.status_code == 409
    message = second.json()["error"]["message"]
    assert "superseded" in message, message
    assert "client_id" not in message, "must not send the client to fix the wrong identity"
