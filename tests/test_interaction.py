"""ADR-0024 / Service UC-02: ordered aggregates and Park tombstones."""

from __future__ import annotations

import json
import pathlib
import uuid

import psycopg

from blocks_service.db import Database
from blocks_service.project_items import InvalidItem, validate_uri


CORPUS = json.loads(
    (pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_3.json").read_text()
)
PARK = {
    "where_i_am": "Panel open",
    "next_physical_action": "Label the third cable",
    "must_not_forget": "Photograph the old terminal",
}


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _actor(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["user_id"]


async def _project(client, auth, tenant, name="Kitchen", who="alice") -> dict:
    client_id = str(uuid.uuid4())
    response = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who),
        json={"client_id": client_id, "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _link(client, auth, tenant, project, title="Link", who="alice") -> dict:
    link_id = str(uuid.uuid4())
    response = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/links", headers=auth(who),
        json={"id": link_id, "title": title, "uri": "https://example.com/",
              "operation_id": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_v03_uri_corpus_has_handler_and_database_parity(client, auth, db: Database):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    link = await _link(client, auth, tenant, project["id"])

    def handler_accepts(value: str) -> bool:
        try:
            validate_uri(value)
            return True
        except InvalidItem:
            return False

    async with db.tenant_transaction(tenant, actor) as conn:
        for expected, values in ((True, CORPUS["accepted"]), (False, CORPUS["refused"])):
            for value in values:
                assert handler_accepts(value) is expected, value
                if "\x00" in value:
                    continue
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "update blocks.project_resource_links set uri = %s where id = %s",
                            (value, link["id"]),
                        )
                        if not expected:
                            raise AssertionError(f"database accepted {value!r}")
                except psycopg.errors.CheckViolation:
                    assert not expected, value


async def test_complete_link_order_replays_and_survives_a_new_client(
    client, auth, db: Database, settings
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    first = await _link(client, auth, tenant, project["id"], "First")
    second = await _link(client, auth, tenant, project["id"], "Second")
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/link-order"
    state = (await client.get(path, headers=auth("alice"))).json()
    operation_id = str(uuid.uuid4())
    envelope = {
        "operation_id": operation_id, "aggregate_id": state["aggregate_id"],
        "base_version": state["version"],
        "ordered_client_ids": [second["id"], first["id"]],
    }

    accepted = await client.put(path, headers=auth("alice"), json=envelope)
    replay = await client.put(path, headers=auth("alice"), json=envelope)
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json() == replay.json()
    assert replay.headers["Idempotent-Replay"] == "true"
    assert (await client.get(path, headers=auth("alice"))).json()["ordered_client_ids"] \
        == [second["id"], first["id"]]
    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/links", headers=auth("alice")
    )
    assert [row["id"] for row in listed.json()] == [second["id"], first["id"]]

    async with db.tenant_transaction(tenant, actor) as conn:
        positions = await (await conn.execute(
            "select link_id::text as id, position from blocks.link_order_positions"
            " where project_id = %s order by position", (project["id"],)
        )).fetchall()
        events = await (await conn.execute(
            "select payload from blocks.outbox where event_type = 'project.link_order_changed'"
        )).fetchall()
    assert [row["id"] for row in positions] == [second["id"], first["id"]]
    assert len(events) == 1
    assert set(events[0]["payload"]) == {"aggregateId", "projectId", "version", "operationId"}

    # A new application instance has no in-memory result to help it. The durable
    # receipt must still replay the same acknowledgement and refuse a reused id.
    from httpx import ASGITransport, AsyncClient
    from blocks_service.app import create_app

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://restarted.test"
        ) as other_client:
            after_restart = await other_client.put(path, headers=auth("alice"), json=envelope)
            assert after_restart.status_code == 200
            assert after_restart.headers["Idempotent-Replay"] == "true"
            assert after_restart.json() == accepted.json()
            reused = await other_client.put(path, headers=auth("alice"), json={
                **envelope, "ordered_client_ids": [first["id"], second["id"]],
            })
            assert reused.status_code == 409


async def test_link_order_rejects_partial_duplicate_stale_and_wrong_project_atomically(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    other = await _project(client, auth, tenant, "Other")
    one = await _link(client, auth, tenant, project["id"], "One")
    two = await _link(client, auth, tenant, project["id"], "Two")
    foreign = await _link(client, auth, tenant, other["id"], "Foreign")
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/link-order"
    before = (await client.get(path, headers=auth("alice"))).json()

    for sequence, version in [
        ([one["id"]], before["version"]),
        ([one["id"], one["id"]], before["version"]),
        ([one["id"], foreign["id"]], before["version"]),
        ([two["id"], one["id"]], before["version"] - 1),
    ]:
        refused = await client.put(path, headers=auth("alice"), json={
            "operation_id": str(uuid.uuid4()), "aggregate_id": before["aggregate_id"],
            "base_version": version, "ordered_client_ids": sequence,
        })
        assert refused.status_code in (400, 409)
        assert (await client.get(path, headers=auth("alice"))).json() == before


async def test_active_project_order_tracks_create_archive_reopen_and_reorder(client, auth):
    tenant = await _tenant(client, auth)
    first = await _project(client, auth, tenant, "First")
    second = await _project(client, auth, tenant, "Second")
    path = f"/v1/tenants/{tenant}/interaction/project-order"
    state = (await client.get(path, headers=auth("alice"))).json()
    assert state["ordered_client_ids"] == [first["client_id"], second["client_id"]]

    moved = await client.put(path, headers=auth("alice"), json={
        "operation_id": str(uuid.uuid4()), "aggregate_id": state["aggregate_id"],
        "base_version": state["version"],
        "ordered_client_ids": [second["client_id"], first["client_id"]],
    })
    assert moved.status_code == 200
    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    assert [row["client_id"] for row in listed.json()] == [second["client_id"], first["client_id"]]

    archived = await client.post(
        f"/v1/tenants/{tenant}/projects/{second['id']}/archive",
        headers={**auth("alice"), "If-Match": "1"},
    )
    assert archived.status_code == 200
    assert (await client.get(path, headers=auth("alice"))).json()["ordered_client_ids"] \
        == [first["client_id"]]
    reopened = await client.post(
        f"/v1/tenants/{tenant}/projects/{second['id']}/reopen",
        headers={**auth("alice"), "If-Match": "2"},
    )
    assert reopened.status_code == 200
    assert (await client.get(path, headers=auth("alice"))).json()["ordered_client_ids"] \
        == [first["client_id"], second["client_id"]]


async def test_park_delete_hides_history_preserves_authored_row_and_replays(client, auth, db: Database):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    created = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/parking",
        headers=auth("alice"), json={**PARK, "client_id": str(uuid.uuid4())},
    )
    assert created.status_code == 201, created.text
    park = created.json()
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/parking/{park['id']}/delete"
    body = {
        "operation_id": str(uuid.uuid4()), "aggregate_id": park["id"],
        "target_id": park["id"], "base_version": park["row_version"],
    }
    before = {key: park[key] for key in (
        "id", "client_id", "project_id", "where_i_am", "next_physical_action",
        "must_not_forget", "authored_by_user_id", "occurred_at", "created_at",
    )}

    deleted = await client.post(path, headers=auth("alice"), json=body)
    replay = await client.post(path, headers=auth("alice"), json=body)
    assert deleted.status_code == replay.status_code == 200
    assert deleted.json() == replay.json() and replay.headers["Idempotent-Replay"] == "true"
    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    history = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}/parking", headers=auth("alice")
    )
    tombstones = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/parking-tombstones",
        headers=auth("alice"),
    )
    assert page.json()["newest_parking"] is None and history.json() == []
    assert tombstones.json()[0]["target_id"] == park["id"]

    async with db.tenant_transaction(tenant, actor) as conn:
        row = await (await conn.execute(
            "select * from blocks.parking_records where id = %s", (park["id"],)
        )).fetchone()
        event = await (await conn.execute(
            "select payload from blocks.outbox where event_type = 'project.parking_deleted'"
        )).fetchone()
        receipt_count = (await (await conn.execute(
            "select count(*) as n from blocks.interaction_operations where operation_id = %s",
            (body["operation_id"],),
        )).fetchone())["n"]
    assert {key: str(row[key]) if key in {"id", "client_id", "project_id", "authored_by_user_id"}
            else row[key].isoformat().replace("+00:00", "Z")
            if key in {"occurred_at", "created_at"} else row[key]
            for key in before} == before
    assert row["deleted_at"] is not None and row["row_version"] == 2
    assert set(event["payload"]) == {"projectId", "parkingRecordId", "version", "operationId"}
    assert receipt_count == 1


async def test_park_delete_absent_foreign_and_wrong_project_share_no_change_shape(client, auth):
    alice = await _tenant(client, auth, "alice")
    bob = await _tenant(client, auth, "bob")
    alice_project = await _project(client, auth, alice, "Alice", "alice")
    other = await _project(client, auth, alice, "Other", "alice")
    bob_project = await _project(client, auth, bob, "Bob", "bob")
    bob_park = (await client.post(
        f"/v1/tenants/{bob}/projects/{bob_project['id']}/parking", headers=auth("bob"),
        json={**PARK, "client_id": str(uuid.uuid4())},
    )).json()
    cases = [str(uuid.uuid4()), bob_park["id"]]
    local_park = (await client.post(
        f"/v1/tenants/{alice}/projects/{other['id']}/parking", headers=auth("alice"),
        json={**PARK, "client_id": str(uuid.uuid4())},
    )).json()
    cases.append(local_park["id"])
    shapes = []
    for target in cases:
        response = await client.post(
            f"/v1/tenants/{alice}/projects/{alice_project['id']}/parking/{target}/delete",
            headers=auth("alice"), json={
                "operation_id": str(uuid.uuid4()), "aggregate_id": target,
                "target_id": target, "base_version": 1,
            },
        )
        assert response.status_code == 200
        shapes.append({key: value for key, value in response.json().items()
                       if key not in {"operation_id", "aggregate_id", "target_id"}})
    assert shapes[0] == shapes[1] == shapes[2]
