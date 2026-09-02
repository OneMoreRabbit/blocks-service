"""The map, physical blocks, idempotency, the change feed, and version gating."""

from __future__ import annotations

import psycopg
import pytest

from tests.test_operating_loop import _pickup, _project, _tenant

PARK = {
    "where_i_am": "here",
    "next_physical_action": "do the next thing",
    "must_not_forget": "the board is live",
}


# --------------------------------------------------------------------------
# The map — ordered, and dateless by construction
# --------------------------------------------------------------------------


async def test_the_map_table_has_no_date_column(settings):
    """Invariant 1, asserted against the schema rather than trusted to review.

    A nullable planned-date column would be written to eventually. The invariant
    holds because there is nowhere to put one.
    """
    from tests.conftest import ADMIN_DSN, _swap_dbname

    with psycopg.connect(_swap_dbname(ADMIN_DSN, settings.db_name), autocommit=True) as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "select column_name from information_schema.columns"
                " where table_schema = 'blocks' and table_name = 'map_items'"
            ).fetchall()
        }
    forbidden = {c for c in columns if any(w in c for w in ("due", "planned", "target", "deadline"))}
    assert not forbidden, f"map_items gained a date-like column: {forbidden}"
    assert columns == {
        "id", "tenant_id", "project_id", "title", "status",
        "sort_key", "row_version", "created_at", "updated_at",
    }


async def test_items_append_in_order(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    for title in ("First fix", "Second fix", "Test and certify"):
        r = await client.post(
            f"/v1/tenants/{tenant}/projects/{project}/map",
            headers=auth("alice"),
            json={"title": title},
        )
        assert r.status_code == 201

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project}/map", headers=auth("alice")
    )
    assert [i["title"] for i in listed.json()] == ["First fix", "Second fix", "Test and certify"]


async def test_an_item_can_be_inserted_between_two_others(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    ids = []
    for title in ("First fix", "Test and certify"):
        r = await client.post(
            f"/v1/tenants/{tenant}/projects/{project}/map",
            headers=auth("alice"),
            json={"title": title},
        )
        ids.append(r.json()["id"])

    r = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/map",
        headers=auth("alice"),
        json={"title": "Second fix", "after_id": ids[0]},
    )
    assert r.status_code == 201

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project}/map", headers=auth("alice")
    )
    assert [i["title"] for i in listed.json()] == ["First fix", "Second fix", "Test and certify"]


async def test_map_edits_use_optimistic_concurrency(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    item = (
        await client.post(
            f"/v1/tenants/{tenant}/projects/{project}/map",
            headers=auth("alice"),
            json={"title": "First fix"},
        )
    ).json()

    ok = await client.patch(
        f"/v1/tenants/{tenant}/projects/{project}/map/{item['id']}",
        headers={**auth("alice"), "If-Match": str(item["row_version"])},
        json={"status": "done"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"
    assert ok.json()["row_version"] == item["row_version"] + 1

    stale = await client.patch(
        f"/v1/tenants/{tenant}/projects/{project}/map/{item['id']}",
        headers={**auth("alice"), "If-Match": str(item["row_version"])},
        json={"status": "dropped"},
    )
    assert stale.status_code == 409


# --------------------------------------------------------------------------
# Physical blocks and NFC pickup
# --------------------------------------------------------------------------


async def test_a_block_can_be_registered_bound_and_picked_up_by_tag(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)

    registered = await client.post(
        f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={"label": "Block 7"}
    )
    assert registered.status_code == 201
    block, token = registered.json(), registered.json()["tag_token"]

    bound = await client.post(
        f"/v1/tenants/{tenant}/blocks/{block['id']}/binding",
        headers=auth("alice"),
        json={"project_id": project},
    )
    assert bound.status_code == 201

    resolved = await client.post(
        f"/v1/tenants/{tenant}/blocks/lookup", headers=auth("alice"), json={"tag_token": token}
    )
    assert resolved.json()["project_id"] == project

    started = await _pickup(
        client, auth, tenant, None, tag_token=token, project_id=None
    )
    assert started.status_code == 201
    assert started.json()["project_id"] == project
    assert started.json()["physical_block_id"] == block["id"]


async def test_the_tag_token_is_never_stored_in_plaintext(client, auth, settings):
    """A database disclosure must not yield working tags."""
    from tests.conftest import ADMIN_DSN, _swap_dbname

    tenant = await _tenant(client, auth)
    registered = await client.post(
        f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={"label": "Block 7"}
    )
    token = registered.json()["tag_token"]

    with psycopg.connect(_swap_dbname(ADMIN_DSN, settings.db_name)) as conn:
        # FORCE RLS applies to the owner too, so even this read needs a tenant
        # context. That is the property under test elsewhere; here it is just the
        # cost of looking at the stored bytes at all.
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        rows = conn.execute("select token_hash from blocks.physical_blocks").fetchall()
    assert rows
    for (stored,) in rows:
        assert token.encode() not in bytes(stored)
        assert len(bytes(stored)) == 32  # sha256


async def test_a_tag_from_another_tenant_does_not_resolve(client, auth):
    """Tokens are globally unique, but resolution is tenant-scoped."""
    alice_tenant = await _tenant(client, auth, "alice")
    registered = await client.post(
        f"/v1/tenants/{alice_tenant}/blocks", headers=auth("alice"), json={"label": "Block 7"}
    )
    token = registered.json()["tag_token"]

    bob_tenant = await _tenant(client, auth, "bob")
    r = await client.post(
        f"/v1/tenants/{bob_tenant}/blocks/lookup", headers=auth("bob"), json={"tag_token": token}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


async def test_a_replayed_operation_is_applied_once(client, auth):
    """The offline queue reconnecting and retrying."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    headers = {**auth("alice"), "Idempotency-Key": "queued-op-1"}
    body = {"project_id": project, "core_outcome": "Rough in the sockets"}

    first = await client.post(f"/v1/tenants/{tenant}/sessions", headers=headers, json=body)
    second = await client.post(f"/v1/tenants/{tenant}/sessions", headers=headers, json=body)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"

    listed = await client.get(f"/v1/tenants/{tenant}/sessions/current", headers=auth("alice"))
    assert listed.json()["session"]["id"] == first.json()["id"]


async def test_a_key_reused_for_a_different_request_is_refused(client, auth):
    """Two distinct operations sharing a key is a client bug, not a retry.

    Replaying the first response would silently discard the second operation.
    """
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    headers = {**auth("alice"), "Idempotency-Key": "queued-op-1"}

    first = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=headers,
        json={"project_id": project, "core_outcome": "Rough in the sockets"},
    )
    assert first.status_code == 201

    clash = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=headers,
        json={"project_id": project, "core_outcome": "Something else entirely"},
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"


async def test_without_a_key_a_repeat_is_a_second_operation(client, auth):
    """Idempotency is opt-in per operation; absence must not silently dedupe."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    body = {"name": "Kitchen rewire"}

    a = await client.post(f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json=body)
    b = await client.post(f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json=body)
    assert a.json()["id"] != b.json()["id"]


# --------------------------------------------------------------------------
# Incremental synchronization
# --------------------------------------------------------------------------


async def test_changes_returns_events_after_the_cursor(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    await _pickup(client, auth, tenant, project)

    first = await client.get(f"/v1/tenants/{tenant}/changes", headers=auth("alice"))
    assert first.status_code == 200
    types = [e["eventType"] for e in first.json()["events"]]
    assert types == ["project.created", "session.started"]

    cursor = first.json()["next_since"]
    again = await client.get(
        f"/v1/tenants/{tenant}/changes?since={cursor}", headers=auth("alice")
    )
    assert again.json()["events"] == []


async def test_changes_paginate(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    await _pickup(client, auth, tenant, project)

    page = await client.get(f"/v1/tenants/{tenant}/changes?limit=1", headers=auth("alice"))
    assert len(page.json()["events"]) == 1
    assert page.json()["has_more"] is True


async def test_changes_are_tenant_scoped(client, auth):
    """The feed is the outbox, so it inherits the outbox's RLS policy."""
    alice_tenant = await _tenant(client, auth, "alice")
    await _project(client, auth, alice_tenant)
    bob_tenant = await _tenant(client, auth, "bob")

    bobs = await client.get(f"/v1/tenants/{bob_tenant}/changes", headers=auth("bob"))
    assert bobs.json()["events"] == []


# --------------------------------------------------------------------------
# Minimum supported client version
# --------------------------------------------------------------------------


async def test_a_client_below_the_minimum_is_refused(client, auth, settings):
    tenant = await _tenant(client, auth)
    settings_min = settings.min_client_version
    client._transport.app.state.settings = settings.model_copy(
        update={"min_client_version": "2.0.0"}
    )
    try:
        r = await client.get(
            f"/v1/tenants/{tenant}/changes",
            headers={**auth("alice"), "X-Blocks-Client-Version": "1.4.0"},
        )
        assert r.status_code == 426
        assert r.json()["error"]["code"] == "upgrade_required"

        ok = await client.get(
            f"/v1/tenants/{tenant}/changes",
            headers={**auth("alice"), "X-Blocks-Client-Version": "2.0.1"},
        )
        assert ok.status_code == 200
    finally:
        client._transport.app.state.settings = settings.model_copy(
            update={"min_client_version": settings_min}
        )


async def test_a_client_that_sends_no_version_is_allowed(client, auth):
    """The header is additive; refusing on absence would break every client."""
    tenant = await _tenant(client, auth)
    r = await client.get(f"/v1/tenants/{tenant}/changes", headers=auth("alice"))
    assert r.status_code == 200


async def test_an_unparseable_version_is_refused(client, auth):
    """"Cannot tell" is not "old enough"."""
    tenant = await _tenant(client, auth)
    r = await client.get(
        f"/v1/tenants/{tenant}/changes",
        headers={**auth("alice"), "X-Blocks-Client-Version": "banana"},
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Association lifecycle: bind, reassociate, unbind
# --------------------------------------------------------------------------


async def _block(client, auth, tenant, label="Block 7") -> dict:
    return (
        await client.post(
            f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={"label": label}
        )
    ).json()


async def test_rebinding_moves_the_block_and_keeps_at_most_one_current(client, auth):
    tenant = await _tenant(client, auth)
    kitchen = await _project(client, auth, tenant, name="Kitchen")
    loft = await _project(client, auth, tenant, name="Loft")
    block = await _block(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/blocks/{block['id']}/binding"

    assert (
        await client.post(path, headers=auth("alice"), json={"project_id": kitchen})
    ).status_code == 201
    assert (
        await client.post(path, headers=auth("alice"), json={"project_id": loft})
    ).status_code == 201

    resolved = await client.post(
        f"/v1/tenants/{tenant}/blocks/lookup",
        headers=auth("alice"),
        json={"tag_token": block["tag_token"]},
    )
    assert resolved.json()["project_id"] == loft

    listed = await client.get(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"))
    assert [b["bound_project_id"] for b in listed.json()] == [loft]


async def test_a_project_may_hold_several_blocks(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    for label in ("Block 7", "Block 8"):
        block = await _block(client, auth, tenant, label)
        await client.post(
            f"/v1/tenants/{tenant}/blocks/{block['id']}/binding",
            headers=auth("alice"),
            json={"project_id": project},
        )

    listed = await client.get(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"))
    assert [b["bound_project_id"] for b in listed.json()] == [project, project]


async def test_unbinding_leaves_the_block_registered_and_resolvable(client, auth):
    """A block can be taken out of service without being destroyed."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    block = await _block(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/blocks/{block['id']}/binding"
    await client.post(path, headers=auth("alice"), json={"project_id": project})

    assert (await client.delete(path, headers=auth("alice"))).status_code == 204

    resolved = await client.post(
        f"/v1/tenants/{tenant}/blocks/lookup",
        headers=auth("alice"),
        json={"tag_token": block["tag_token"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["project_id"] is None


async def test_unbinding_an_unbound_block_is_not_found(client, auth):
    tenant = await _tenant(client, auth)
    block = await _block(client, auth, tenant)
    r = await client.delete(
        f"/v1/tenants/{tenant}/blocks/{block['id']}/binding", headers=auth("alice")
    )
    assert r.status_code == 404


async def test_picking_up_an_unbound_tag_is_refused(client, auth):
    """The tag resolves, but there is no project to pick up — say so, don't guess."""
    tenant = await _tenant(client, auth)
    block = await _block(client, auth, tenant)
    r = await client.post(
        f"/v1/tenants/{tenant}/sessions",
        headers=auth("alice"),
        json={"tag_token": block["tag_token"], "core_outcome": "x"},
    )
    assert r.status_code == 400
