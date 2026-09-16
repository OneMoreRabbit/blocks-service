"""Discrete project items: Notes, resource Links, navigation aliases (API 0.6).

ADR-0021 and ADR-0022, built against Android's published project-items boundary.

The through-line is the same one API 0.5 established for tags: the client names
the resource, the name is scoped by tenant so it cannot be probed, and the server
keeps enough to recognise a replay long after the idempotency result has gone.
What is new is that authored text now exists in quantity -- note bodies, link
titles, URIs -- and none of it may reach an event, a log or an error.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from blocks_service.db import Database


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _actor(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["user_id"]


async def _project(client, auth, tenant, who="alice", name="Kitchen rewire") -> str:
    return (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who), json={"name": name}
    )).json()["id"]


async def _note(client, auth, tenant, project, who="alice", **kw):
    body = {"id": str(uuid.uuid4()), "body": "Wire the third box",
            "operation_id": str(uuid.uuid4())} | kw
    return await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes", headers=auth(who), json=body
    ), body


async def _link(client, auth, tenant, project, who="alice", **kw):
    body = {"id": str(uuid.uuid4()), "title": "Wiring regs",
            "uri": "https://example.com/regs", "operation_id": str(uuid.uuid4())} | kw
    return await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/links", headers=auth(who), json=body
    ), body


def _vh(auth, who, version):
    return {**auth(who), "If-Match": str(version)}


async def _expire_results(db: Database, tenant: str, actor: str) -> int:
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("delete from blocks.idempotency_keys")
        return cur.rowcount


# --------------------------------------------------------------------------
# Creation identity, replay and conflict
# --------------------------------------------------------------------------


async def test_a_note_is_created_under_the_clients_own_id(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r, sent = await _note(client, auth, tenant, project)

    assert r.status_code == 201
    body = r.json()
    assert body["id"] == sent["id"], "the client's id IS the item identity"
    assert body["operation_id"] == sent["operation_id"], "echoed for retirement"
    assert body["row_version"] == 1
    assert body["deleted_at"] is None
    assert body["authored_by_user_id"], "author derived, not supplied"


async def test_an_identical_create_replays_and_a_different_one_conflicts(
    client, auth, db: Database
):
    """First creation wins; never last-write-wins — after retention expires too."""
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    project = await _project(client, auth, tenant)
    first, sent = await _note(client, auth, tenant, project)
    assert first.status_code == 201

    await _expire_results(db, tenant, actor)

    same = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes", headers=auth("alice"), json=sent)
    assert same.status_code == 201, "convergent by (tenant, client id) after expiry"
    assert same.json()["id"] == first.json()["id"]

    clash = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes", headers=auth("alice"),
        json={**sent, "body": "Something else"})
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes", headers=auth("alice"))
    assert len(listed.json()) == 1, "the conflict committed nothing"
    assert listed.json()[0]["body"] == "Wire the third box", "first creation wins"


async def test_the_same_item_id_in_another_tenant_is_independent(client, auth):
    """Tenant-scoped identity: no global index, so nothing to probe."""
    alice, bob = await _tenant(client, auth, "alice"), await _tenant(client, auth, "bob")
    pa, pb = await _project(client, auth, alice), await _project(client, auth, bob, "bob")
    shared = str(uuid.uuid4())

    mine, _ = await _note(client, auth, alice, pa, id=shared)
    theirs, _ = await _note(client, auth, bob, pb, "bob", id=shared)

    assert mine.status_code == 201
    assert theirs.status_code == 201, "same UUID, different tenants, no collision"


# --------------------------------------------------------------------------
# Optimistic concurrency and tombstones
# --------------------------------------------------------------------------


async def test_edit_pin_and_delete_are_version_checked(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    created, sent = await _note(client, auth, tenant, project)
    item, base = created.json()["id"], created.json()["row_version"]
    path = f"/v1/tenants/{tenant}/projects/{project}/items/notes/{item}"

    edited = await client.patch(path, headers=_vh(auth, "alice", base),
                                json={"body": "Now four boxes",
                                      "operation_id": str(uuid.uuid4())})
    assert edited.status_code == 200
    assert edited.json()["row_version"] == base + 1

    stale = await client.patch(path, headers=_vh(auth, "alice", base),
                               json={"body": "racing", "operation_id": str(uuid.uuid4())})
    assert stale.status_code == 409, "a stale If-Match is a real conflict"

    pinned = await client.put(f"{path}/pin", headers=_vh(auth, "alice", base + 1),
                              json={"pinned": True, "operation_id": str(uuid.uuid4())})
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True

    gone = await client.post(f"{path}/delete", headers=_vh(auth, "alice", base + 2),
                             json={"operation_id": str(uuid.uuid4())})
    assert gone.status_code == 200
    assert gone.json()["deleted_at"], "a tombstone, not a removal"


async def test_a_tombstoned_note_still_appears_in_reads(client, auth):
    """A delete a client has not seen must be learnable; absence would hide it."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    created, _ = await _note(client, auth, tenant, project)
    item = created.json()["id"]
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes/{item}/delete",
        headers=_vh(auth, "alice", 1), json={"operation_id": str(uuid.uuid4())})

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes", headers=auth("alice"))
    assert len(listed.json()) == 1
    assert listed.json()[0]["deleted_at"], "tombstones are part of the pull"


async def test_the_row_is_retained_in_the_database_after_delete(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    project = await _project(client, auth, tenant)
    created, _ = await _note(client, auth, tenant, project)
    await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes/{created.json()['id']}/delete",
        headers=_vh(auth, "alice", 1), json={"operation_id": str(uuid.uuid4())})

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select count(*) as n from blocks.project_note_items")
        assert (await cur.fetchone())["n"] == 1, "the row survives; only deleted_at is set"
        with pytest.raises(psycopg.errors.Error):
            async with conn.transaction():
                await conn.execute("delete from blocks.project_note_items")


# --------------------------------------------------------------------------
# Absent and foreign are one answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["absent", "foreign"])
async def test_an_unknown_project_and_a_foreign_one_answer_alike(client, auth, case):
    alice = await _tenant(client, auth, "alice")
    if case == "absent":
        target = str(uuid.uuid4())
    else:
        bob = await _tenant(client, auth, "bob")
        target = await _project(client, auth, bob, "bob")

    r, _ = await _note(client, auth, alice, target)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("case", ["absent", "foreign"])
async def test_an_unknown_item_and_a_foreign_one_answer_alike(client, auth, case):
    alice = await _tenant(client, auth, "alice")
    pa = await _project(client, auth, alice)
    if case == "absent":
        item = str(uuid.uuid4())
    else:
        bob = await _tenant(client, auth, "bob")
        pb = await _project(client, auth, bob, "bob")
        item = (await _note(client, auth, bob, pb, "bob"))[0].json()["id"]

    r = await client.patch(
        f"/v1/tenants/{alice}/projects/{pa}/items/notes/{item}",
        headers=_vh(auth, "alice", 1),
        json={"body": "x", "operation_id": str(uuid.uuid4())})
    assert r.status_code == 404, "a foreign item is indistinguishable from an absent one"


# --------------------------------------------------------------------------
# Authored text never leaves the row
# --------------------------------------------------------------------------


async def test_no_authored_text_reaches_an_event(client, auth, db: Database):
    """Bodies, titles and URIs are user content; the change feed carries ids."""
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    project = await _project(client, auth, tenant)
    secret_body = "MEETING NOTES: the boiler is behind the panel"
    secret_title = "Private supplier pricing"
    secret_uri = "https://example.com/secret-tender-document"

    n, _ = await _note(client, auth, tenant, project, body=secret_body)
    l, _ = await _link(client, auth, tenant, project, title=secret_title, uri=secret_uri)
    assert n.status_code == 201 and l.status_code == 201

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select event_type, payload::text as p from blocks.outbox")
        rows = await cur.fetchall()

    feed = str(rows)
    assert secret_body not in feed
    assert secret_title not in feed
    assert secret_uri not in feed
    types = {r["event_type"] for r in rows}
    assert {"project.note_created", "project.link_created"} <= types
    for r in rows:
        if r["event_type"].startswith("project.note_") or r["event_type"].startswith("project.link_"):
            assert "operationId" in r["p"] and "itemId" in r["p"]


async def test_an_error_does_not_quote_the_rejected_content(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    secret = "https://internal.example.com/confidential"

    r, _ = await _link(client, auth, tenant, project, uri="file:///etc/passwd")
    assert r.status_code == 400
    bad = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/links", headers=auth("alice"),
        json={"id": str(uuid.uuid4()), "title": "t", "uri": secret.replace("https", "ftp"),
              "operation_id": str(uuid.uuid4())})
    assert bad.status_code == 400
    assert "confidential" not in bad.text, "a refusal does not echo the value it refused"


# --------------------------------------------------------------------------
# Link URI boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("uri", [
    "https://example.com/a", "http://example.com/a", "obsidian://open?vault=v&file=f",
])
async def test_allowed_uri_schemes_are_stored_unchanged(client, auth, uri):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r, _ = await _link(client, auth, tenant, project, uri=uri)
    assert r.status_code == 201
    assert r.json()["uri"] == uri, "the accepted logical value is returned unchanged"


@pytest.mark.parametrize("uri", [
    "file:///etc/passwd", "content://media/x", "intent://scan#Intent;end",
    "javascript:alert(1)", "ftp://example.com/x", "https://user:pw@example.com/x",
    "https://", "", "   ", "https://example.com/\x00", "not-a-uri",
])
async def test_refused_uri_forms(client, auth, uri):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r, _ = await _link(client, auth, tenant, project, uri=uri)
    assert r.status_code == 400, uri


# --------------------------------------------------------------------------
# The clean break from singleton Notes (ADR-0022)
# --------------------------------------------------------------------------


async def test_the_api_0_4_singleton_and_0_6_items_do_not_touch_each_other(
    client, auth
):
    """Coexistence, proven in both directions rather than asserted."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    notes_path = f"/v1/tenants/{tenant}/projects/{project}/items/notes"

    singleton = await client.get(f"/v1/tenants/{tenant}/projects/{project}",
                                 headers=auth("alice"))
    assert singleton.status_code == 200
    base = singleton.json()["notes"]["row_version"]

    # A 0.4 singleton write creates no discrete item.
    saved = await client.put(
        f"/v1/tenants/{tenant}/projects/{project}/notes",   # the API 0.4 singleton
        headers={**auth("alice"), "If-Match": str(base)},
        json={"body": "singleton content"})
    if saved.status_code == 200:
        listed = await client.get(notes_path, headers=auth("alice"))
        assert listed.json() == [], "a 0.4 notes save creates no 0.6 item"

    # A 0.6 item create does not alter the singleton.
    created, _ = await _note(client, auth, tenant, project, body="discrete item")
    assert created.status_code == 201
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}",
                            headers=auth("alice"))
    assert "discrete item" not in str(page.json()["notes"]), (
        "the discrete collection never projects into the singleton row"
    )


# --------------------------------------------------------------------------
# Navigation aliases
# --------------------------------------------------------------------------


async def test_a_navigation_key_attaches_resolves_and_is_not_the_client_id(
    client, auth
):
    tenant = await _tenant(client, auth)
    cid = str(uuid.uuid4())
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"),
        json={"name": "Kitchen", "client_id": cid})).json()["id"]
    key = str(uuid.uuid4())

    attached = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
        headers=auth("alice"), json={"project_key": key})
    assert attached.status_code == 201
    assert attached.json()["project_key"] == key
    assert key != cid, "a navigation key is a distinct identity from client_id"

    again = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
        headers=auth("alice"), json={"project_key": key})
    assert again.status_code == 200, "attach is idempotent by tenant, project and key"

    resolved = await client.post(
        f"/v1/tenants/{tenant}/projects/resolve-key",
        headers=auth("alice"), json={"project_key": key})
    assert resolved.status_code == 200
    assert resolved.json()["project_id"] == project


async def test_the_creation_identity_is_never_rotated_into_a_navigation_key(
    client, auth
):
    tenant = await _tenant(client, auth)
    cid = str(uuid.uuid4())
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"),
        json={"name": "Kitchen", "client_id": cid})).json()["id"]
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
                      headers=auth("alice"), json={"project_key": key})

    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}",
                            headers=auth("alice"))
    assert page.json()["project"]["client_id"] == cid, (
        "attaching a navigation key leaves the creation identity untouched"
    )


async def test_the_same_key_on_another_project_conflicts_without_rotating(client, auth):
    tenant = await _tenant(client, auth)
    a, b = await _project(client, auth, tenant, name="A"), await _project(client, auth, tenant, name="B")
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{tenant}/projects/{a}/navigation-aliases",
                      headers=auth("alice"), json={"project_key": key})

    clash = await client.post(f"/v1/tenants/{tenant}/projects/{b}/navigation-aliases",
                              headers=auth("alice"), json={"project_key": key})
    assert clash.status_code == 409, "one declared rule, never a silent rotation"

    still = await client.post(f"/v1/tenants/{tenant}/projects/resolve-key",
                              headers=auth("alice"), json={"project_key": key})
    assert still.json()["project_id"] == a, "the printed key still opens what it opened"


async def test_a_key_from_another_tenant_resolves_exactly_as_an_unknown_one(
    client, auth
):
    alice, bob = await _tenant(client, auth, "alice"), await _tenant(client, auth, "bob")
    pb = await _project(client, auth, bob, "bob")
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{bob}/projects/{pb}/navigation-aliases",
                      headers=auth("bob"), json={"project_key": key})

    foreign = await client.post(f"/v1/tenants/{alice}/projects/resolve-key",
                                headers=auth("alice"), json={"project_key": key})
    unknown = await client.post(f"/v1/tenants/{alice}/projects/resolve-key",
                                headers=auth("alice"), json={"project_key": str(uuid.uuid4())})
    assert foreign.status_code == 404
    assert foreign.json() == unknown.json(), "no cross-tenant existence oracle"


async def test_the_key_is_never_in_an_event(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    project = await _project(client, auth, tenant)
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
                      headers=auth("alice"), json={"project_key": key})

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select payload::text as p from blocks.outbox")
        assert key not in str(await cur.fetchall())
