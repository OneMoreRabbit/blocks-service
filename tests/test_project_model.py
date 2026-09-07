"""The project model (ADR-0009): dashboard, project page, notes, parking.

What these assert is largely what the product no longer does. Opening a project
is navigation; nothing here starts, resumes or closes anything, and physical-device
UAT rejecting that workflow is why.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from blocks_service.db import Database

PARK = {
    "where_i_am": "Two sockets wired, third box mounted",
    "next_physical_action": "Strip the 2.5mm cable at the third box",
    "must_not_forget": "The circuit is still live at the board",
}


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _project(client, auth, tenant, who="alice", name="Kitchen rewire") -> dict:
    r = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who), json={"name": name}
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------
# Dashboard, archive, reopen
# --------------------------------------------------------------------------


async def test_a_new_project_is_active_with_empty_notes(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    assert project["status"] == "active"
    assert project["archived_at"] is None
    assert project["row_version"] == 1

    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert page.status_code == 200
    assert page.json()["notes"]["body"] == ""
    assert page.json()["newest_parking"] is None


async def test_opening_a_project_changes_nothing(client, auth):
    """Selection is navigation. The workflow UAT rejected made it a mutation."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    before = (await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )).json()["project"]
    for _ in range(3):
        await client.get(f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice"))
    after = (await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )).json()["project"]

    assert before == after
    assert after["row_version"] == 1, "reading must not bump the version"


async def test_two_projects_can_be_browsed_without_conflict(client, auth):
    """No current project, no one-active rule: browsing never conflicts."""
    tenant = await _tenant(client, auth)
    a = await _project(client, auth, tenant, name="Kitchen")
    b = await _project(client, auth, tenant, name="Loft")

    for project in (a, b, a, b):
        r = await client.get(
            f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
        )
        assert r.status_code == 200


async def test_archive_hides_from_the_dashboard_and_reopen_restores(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    archived = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/archive",
        headers={**auth("alice"), "If-Match": str(project["row_version"])},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["archived_at"] is not None

    active = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert [p["id"] for p in active.json()] == []

    archived_list = await client.get(
        f"/v1/tenants/{tenant}/projects?status=archived", headers=auth("alice")
    )
    assert [p["id"] for p in archived_list.json()] == [project["id"]]

    reopened = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/reopen",
        headers={**auth("alice"), "If-Match": str(archived.json()["row_version"])},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "active"
    assert reopened.json()["archived_at"] is None, "reopening must clear the timestamp"


async def test_archiving_is_not_deletion(client, auth):
    """The project, its notes and its parking all survive archiving."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/parking",
        headers=auth("alice"), json=PARK,
    )
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/archive",
        headers={**auth("alice"), "If-Match": "1"},
    )

    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert page.status_code == 200
    assert page.json()["newest_parking"]["must_not_forget"] == PARK["must_not_forget"]


async def test_archiving_twice_conflicts(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    first = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/archive",
        headers={**auth("alice"), "If-Match": "1"},
    )
    again = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/archive",
        headers={**auth("alice"), "If-Match": str(first.json()["row_version"])},
    )
    assert again.status_code == 409


async def test_rename_uses_optimistic_concurrency(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    ok = await client.patch(
        f"/v1/tenants/{tenant}/projects/{project['id']}",
        headers={**auth("alice"), "If-Match": "1"}, json={"name": "Kitchen rewire v2"},
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Kitchen rewire v2"

    stale = await client.patch(
        f"/v1/tenants/{tenant}/projects/{project['id']}",
        headers={**auth("alice"), "If-Match": "1"}, json={"name": "third"},
    )
    assert stale.status_code == 409


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------


async def test_notes_save_read_and_conflict(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/notes"

    saved = await client.put(
        path, headers={**auth("alice"), "If-Match": "1"}, json={"body": "wire colours: brown live"}
    )
    assert saved.status_code == 200
    assert saved.json()["body"] == "wire colours: brown live"
    assert saved.json()["row_version"] == 2

    read = await client.get(path, headers=auth("alice"))
    assert read.json()["body"] == "wire colours: brown live"

    stale = await client.put(
        path, headers={**auth("alice"), "If-Match": "1"}, json={"body": "from the other device"}
    )
    assert stale.status_code == 409, "two devices editing must conflict, not last-write-wins"

    assert (await client.get(path, headers=auth("alice"))).json()["body"] == "wire colours: brown live"


async def test_notes_may_be_emptied(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/notes"
    await client.put(path, headers={**auth("alice"), "If-Match": "1"}, json={"body": "something"})
    cleared = await client.put(path, headers={**auth("alice"), "If-Match": "2"}, json={"body": ""})
    assert cleared.status_code == 200 and cleared.json()["body"] == ""


async def test_notes_never_reach_an_event_payload(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    secret = "SECRET-NOTE-BODY-consumer-fuse-board"

    await client.put(
        f"/v1/tenants/{tenant}/projects/{project['id']}/notes",
        headers={**auth("alice"), "If-Match": "1"}, json={"body": secret},
    )

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select payload::text as p from blocks.outbox")
        payloads = [r["p"] for r in await cur.fetchall()]
    assert payloads, "the save must still announce itself"
    for payload in payloads:
        assert secret not in payload


# --------------------------------------------------------------------------
# Parking on a project
# --------------------------------------------------------------------------


async def test_parking_is_optional_and_newest_first(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/parking"

    assert (await client.get(path, headers=auth("alice"))).json() == []

    for marker in ("older", "newer"):
        r = await client.post(
            path, headers=auth("alice"), json={**PARK, "where_i_am": marker}
        )
        assert r.status_code == 201

    history = await client.get(path, headers=auth("alice"))
    assert [p["where_i_am"] for p in history.json()] == ["newer", "older"]

    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert page.json()["newest_parking"]["where_i_am"] == "newer"


async def test_a_correction_supersedes_rather_than_edits(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/parking"

    original = (await client.post(path, headers=auth("alice"), json=PARK)).json()
    correction = await client.post(
        path, headers=auth("alice"),
        json={**PARK, "where_i_am": "corrected", "supersedes_id": original["id"]},
    )
    assert correction.status_code == 201

    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert page.json()["newest_parking"]["where_i_am"] == "corrected"

    history = await client.get(path, headers=auth("alice"))
    assert len(history.json()) == 2, "history keeps the superseded record"


async def test_parking_is_immutable_to_the_service(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/parking",
        headers=auth("alice"), json=PARK,
    )

    async with db.tenant_transaction(tenant, actor) as conn:
        for sql in (
            "update blocks.parking_records set where_i_am = 'rewritten'",
            "delete from blocks.parking_records",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(sql)


async def test_a_replayed_park_is_appended_once(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/parking"
    headers = {**auth("alice"), "Idempotency-Key": "queued-park-1"}

    first = await client.post(path, headers=headers, json=PARK)
    second = await client.post(path, headers=headers, json=PARK)
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"
    assert len((await client.get(path, headers=auth("alice"))).json()) == 1


async def test_parking_text_never_reaches_an_event(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/parking",
        headers=auth("alice"),
        json={"where_i_am": "SECRET-A", "next_physical_action": "SECRET-B", "must_not_forget": "SECRET-C"},
    )
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select payload::text as p from blocks.outbox")
        payloads = " ".join(r["p"] for r in await cur.fetchall())
    for secret in ("SECRET-A", "SECRET-B", "SECRET-C"):
        assert secret not in payloads


# --------------------------------------------------------------------------
# NFC resolution — equivalent to manual, and never a mutation
# --------------------------------------------------------------------------


async def test_tag_resolution_matches_manual_selection_and_mutates_nothing(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    block = (await client.post(
        f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={"label": "Block 7"}
    )).json()
    await client.post(
        f"/v1/tenants/{tenant}/blocks/{block['id']}/binding",
        headers=auth("alice"), json={"project_id": project["id"]},
    )

    resolved = await client.post(
        f"/v1/tenants/{tenant}/blocks/lookup",
        headers=auth("alice"), json={"tag_token": block["tag_token"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["project_id"] == project["id"]

    by_tag = await client.get(
        f"/v1/tenants/{tenant}/projects/{resolved.json()['project_id']}", headers=auth("alice")
    )
    by_hand = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert by_tag.json() == by_hand.json(), "NFC and manual selection must be the same page"
    assert by_tag.json()["project"]["row_version"] == 1, "scanning must not mutate state"


# --------------------------------------------------------------------------
# Tenant isolation across every new surface
# --------------------------------------------------------------------------


async def test_no_project_surface_crosses_a_tenant(client, auth):
    alice_tenant = await _tenant(client, auth, "alice")
    project = await _project(client, auth, alice_tenant)
    await client.post(
        f"/v1/tenants/{alice_tenant}/projects/{project['id']}/parking",
        headers=auth("alice"), json=PARK,
    )
    await _tenant(client, auth, "bob")

    base = f"/v1/tenants/{alice_tenant}/projects"
    for method, path, body in [
        ("get", base, None),
        ("get", f"{base}/{project['id']}", None),
        ("get", f"{base}/{project['id']}/notes", None),
        ("get", f"{base}/{project['id']}/parking", None),
        ("post", f"{base}/{project['id']}/parking", PARK),
        ("post", f"{base}/{project['id']}/archive", None),
    ]:
        call = getattr(client, method)
        kwargs = {"headers": {**auth("bob"), "If-Match": "1"}}
        if body is not None:
            kwargs["json"] = body
        r = await call(path, **kwargs)
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"


async def test_a_project_from_another_tenant_is_not_found(client, auth):
    """Within a tenant the caller belongs to, another tenant's id is simply absent."""
    alice_tenant = await _tenant(client, auth, "alice")
    project = await _project(client, auth, alice_tenant)
    bob_tenant = await _tenant(client, auth, "bob")

    r = await client.get(
        f"/v1/tenants/{bob_tenant}/projects/{project['id']}", headers=auth("bob")
    )
    assert r.status_code == 404


async def test_notes_cannot_be_written_across_a_tenant_by_foreign_key(client, auth, db: Database):
    """The composite key makes a cross-tenant reference unwritable, not just unreadable."""
    alice_tenant = await _tenant(client, auth, "alice")
    project = await _project(client, auth, alice_tenant)
    bob_tenant = await _tenant(client, auth, "bob")
    bob = (await client.post("/v1/session", headers=auth("bob"))).json()["user_id"]

    async with db.tenant_transaction(bob_tenant, bob) as conn:
        with pytest.raises(psycopg.errors.Error):
            await conn.execute(
                "insert into blocks.project_notes (tenant_id, project_id, body)"
                " values (%s, %s, 'planted')",
                (bob_tenant, project["id"]),
            )


# --------------------------------------------------------------------------
# Idempotent creation — the brief requires it for retryable creates, not only
# appends, and a duplicate project is a worse outcome than a duplicate park.
# --------------------------------------------------------------------------


async def test_a_replayed_project_creation_creates_one_project(client, auth):
    tenant = await _tenant(client, auth)
    headers = {**auth("alice"), "Idempotency-Key": "queued-create-1"}
    body = {"name": "Kitchen rewire"}

    first = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)
    second = await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json(), "the replay must return the stored result"
    assert second.headers.get("Idempotent-Replay") == "true"

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert [p["id"] for p in listed.json()] == [first.json()["id"]]


async def test_a_replayed_creation_does_not_create_a_second_notes_row(client, auth, db: Database):
    """Creation writes a project AND its notes; a replay must duplicate neither."""
    tenant = await _tenant(client, auth)
    actor = (await client.post("/v1/session", headers=auth("alice"))).json()["user_id"]
    headers = {**auth("alice"), "Idempotency-Key": "queued-create-1"}
    body = {"name": "Kitchen rewire"}

    await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)
    await client.post(f"/v1/tenants/{tenant}/projects", headers=headers, json=body)

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select count(*) as n from blocks.project_notes")
        notes = (await cur.fetchone())["n"]
        cur = await conn.execute("select count(*) as n from blocks.outbox where event_type = 'project.created'")
        events = (await cur.fetchone())["n"]
    assert notes == 1
    assert events == 1, "a replay must not re-announce the creation"


async def test_the_same_creation_key_with_a_different_name_is_refused(client, auth):
    tenant = await _tenant(client, auth)
    headers = {**auth("alice"), "Idempotency-Key": "queued-create-1"}

    assert (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=headers, json={"name": "Kitchen"}
    )).status_code == 201

    clash = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=headers, json={"name": "Loft"}
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"


async def test_a_creation_replay_is_scoped_to_its_tenant(client, auth):
    """The same key in another tenant is a different operation, not a replay."""
    alice_tenant = await _tenant(client, auth, "alice")
    bob_tenant = await _tenant(client, auth, "bob")
    body = {"name": "Kitchen rewire"}

    a = await client.post(
        f"/v1/tenants/{alice_tenant}/projects",
        headers={**auth("alice"), "Idempotency-Key": "shared-key"}, json=body,
    )
    b = await client.post(
        f"/v1/tenants/{bob_tenant}/projects",
        headers={**auth("bob"), "Idempotency-Key": "shared-key"}, json=body,
    )
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
