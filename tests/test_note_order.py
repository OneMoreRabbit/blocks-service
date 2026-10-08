"""ADR-0028 / Service UC-05: Note order and browser-link parity."""

from __future__ import annotations

import json
import pathlib
import uuid

import psycopg

from blocks_service.db import Database
from blocks_service.project_items import InvalidItem, validate_uri


CORPUS_PATH = pathlib.Path(__file__).parent / "project-resource-uri-validation-v0_4.json"
CORPUS = json.loads(CORPUS_PATH.read_text())
OWNER_LINK = CORPUS["accepted"][-1]
API_08 = {"X-Blocks-API-Version": "0.8"}


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _actor(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["user_id"]


async def _project(client, auth, tenant, name="Kitchen", who="alice") -> dict:
    response = await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who),
        json={"client_id": str(uuid.uuid4()), "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _note(client, auth, tenant, project, body="Note", who="alice") -> dict:
    response = await client.post(
        f"/v1/tenants/{tenant}/projects/{project}/items/notes",
        headers=auth(who),
        json={"id": str(uuid.uuid4()), "body": body,
              "operation_id": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _envelope(state: dict, ordered: list[str], operation_id: str | None = None) -> dict:
    return {
        "operation_id": operation_id or str(uuid.uuid4()),
        "aggregate_id": state["aggregate_id"],
        "base_version": state["version"],
        "ordered_client_ids": ordered,
    }


async def test_v04_uri_corpus_has_handler_database_and_route_parity(
    client, auth, db: Database,
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    link_id = str(uuid.uuid4())
    created = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/links",
        headers={**auth("alice"), "X-Blocks-API-Version": "0.8"},
        json={"id": link_id, "title": "Owner link", "uri": OWNER_LINK,
              "operation_id": str(uuid.uuid4())},
    )
    assert created.status_code == 201, created.text
    assert created.json()["uri"] == OWNER_LINK

    def handler_accepts(value: str) -> bool:
        try:
            return validate_uri(value, api_version="0.8") == value
        except InvalidItem:
            return False

    async with db.tenant_transaction(tenant, actor) as conn:
        for expected, values in ((True, CORPUS["accepted"]), (False, CORPUS["refused"])):
            for value in values:
                assert handler_accepts(value) is expected, value
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "update blocks.project_resource_links set uri = %s where id = %s",
                            (value, link_id),
                        )
                        if not expected:
                            raise AssertionError(f"database accepted {value!r}")
                except psycopg.errors.CheckViolation:
                    assert not expected, value

    assert validate_uri(OWNER_LINK, api_version="0.7") == OWNER_LINK


async def test_complete_note_order_replays_after_restart_and_emits_identifiers_only(
    client, auth, db: Database, settings,
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    first = await _note(client, auth, tenant, project["id"], "First body")
    second = await _note(client, auth, tenant, project["id"], "Second body")
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/note-order"
    state = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    envelope = _envelope(state, [second["id"], first["id"]])

    accepted = await client.put(path, headers={**auth("alice"), **API_08}, json=envelope)
    replay = await client.put(path, headers={**auth("alice"), **API_08}, json=envelope)
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json() == replay.json()
    assert replay.headers["Idempotent-Replay"] == "true"
    assert accepted.json()["ordered_client_ids"] == [second["id"], first["id"]]

    listed = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/notes",
        headers=auth("alice"),
    )
    assert [row["id"] for row in listed.json() if row["deleted_at"] is None] == [
        second["id"], first["id"],
    ]

    async with db.tenant_transaction(tenant, actor) as conn:
        positions = await (await conn.execute(
            "select note_id::text as id, position from blocks.note_order_positions"
            " where project_id = %s order by position", (project["id"],)
        )).fetchall()
        events = await (await conn.execute(
            "select payload from blocks.outbox where event_type = 'project.note_order_changed'"
        )).fetchall()
        receipts = await (await conn.execute(
            "select result from blocks.interaction_operations"
            " where operation_id = %s", (envelope["operation_id"],)
        )).fetchall()
    assert [row["id"] for row in positions] == [second["id"], first["id"]]
    assert len(events) == len(receipts) == 1
    assert set(events[0]["payload"]) == {
        "aggregateId", "projectId", "version", "operationId",
    }
    assert "ordered_client_ids" not in events[0]["payload"]
    assert "First body" not in str(events[0]["payload"])

    from httpx import ASGITransport, AsyncClient
    from blocks_service.app import create_app

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://restarted.test"
        ) as other_client:
            after_restart = await other_client.put(
                path, headers={**auth("alice"), **API_08}, json=envelope,
            )
            assert after_restart.status_code == 200
            assert after_restart.headers["Idempotent-Replay"] == "true"
            assert after_restart.json() == accepted.json()
            reused = await other_client.put(
                path, headers={**auth("alice"), **API_08},
                json={**envelope, "ordered_client_ids": [first["id"], second["id"]]},
            )
            assert reused.status_code == 409


async def test_note_order_rejects_invalid_membership_atomically_and_non_disclosingly(
    client, auth, db: Database,
):
    alice = await _tenant(client, auth, "alice")
    bob = await _tenant(client, auth, "bob")
    actor = await _actor(client, auth, "alice")
    project = await _project(client, auth, alice, "Primary", "alice")
    other = await _project(client, auth, alice, "Other", "alice")
    bob_project = await _project(client, auth, bob, "Foreign", "bob")
    one = await _note(client, auth, alice, project["id"], "One", "alice")
    two = await _note(client, auth, alice, project["id"], "Two", "alice")
    wrong = await _note(client, auth, alice, other["id"], "Wrong", "alice")
    foreign = await _note(client, auth, bob, bob_project["id"], "Foreign", "bob")
    deleted = await _note(client, auth, alice, project["id"], "Deleted", "alice")
    deleted_response = await client.post(
        f"/v1/tenants/{alice}/projects/{project['id']}/items/notes/{deleted['id']}/delete",
        headers={**auth("alice"), "If-Match": str(deleted["row_version"])},
        json={"operation_id": str(uuid.uuid4())},
    )
    assert deleted_response.status_code == 200

    path = f"/v1/tenants/{alice}/projects/{project['id']}/interaction/note-order"
    before = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    cases = [
        ([one["id"]], before["version"]),
        ([one["id"], one["id"]], before["version"]),
        ([one["id"], wrong["id"]], before["version"]),
        ([one["id"], foreign["id"]], before["version"]),
        ([one["id"], deleted["id"]], before["version"]),
    ]
    bodies = []
    for sequence, version in cases:
        refused = await client.put(
            path, headers={**auth("alice"), **API_08}, json={
                "operation_id": str(uuid.uuid4()),
                "aggregate_id": before["aggregate_id"],
                "base_version": version,
                "ordered_client_ids": sequence,
            },
        )
        assert refused.status_code == 400
        bodies.append(refused.json())
        assert (await client.get(path, headers={**auth("alice"), **API_08})).json() == before
    assert all(body == bodies[0] for body in bodies)

    stale = await client.put(
        path, headers={**auth("alice"), **API_08},
        json=_envelope({**before, "version": before["version"] - 1}, [two["id"], one["id"]]),
    )
    assert stale.status_code == 409
    assert (await client.get(path, headers={**auth("alice"), **API_08})).json() == before

    async with db.tenant_transaction(alice, actor) as conn:
        receipt_count = (await (await conn.execute(
            "select count(*) as n from blocks.interaction_operations"
            " where operation_type = 'REORDER_NOTES'"
        )).fetchone())["n"]
        event_count = (await (await conn.execute(
            "select count(*) as n from blocks.outbox"
            " where event_type = 'project.note_order_changed'"
        )).fetchone())["n"]
    assert receipt_count == event_count == 0


async def test_note_create_appends_edit_and_pin_preserve_and_delete_removes(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    first = await _note(client, auth, tenant, project["id"], "First")
    second = await _note(client, auth, tenant, project["id"], "Second")
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/note-order"
    state = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    moved = await client.put(
        path, headers={**auth("alice"), **API_08},
        json=_envelope(state, [second["id"], first["id"]]),
    )
    assert moved.status_code == 200

    edited = await client.patch(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/notes/{first['id']}",
        headers={**auth("alice"), "If-Match": str(first["row_version"])},
        json={"body": "First edited", "operation_id": str(uuid.uuid4())},
    )
    assert edited.status_code == 200
    pinned = await client.put(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/notes/{second['id']}/pin",
        headers={**auth("alice"), "If-Match": str(second["row_version"])},
        json={"pinned": True, "operation_id": str(uuid.uuid4())},
    )
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True
    after_mutations = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    assert after_mutations == moved.json()

    third = await _note(client, auth, tenant, project["id"], "Third")
    after_create = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    assert after_create["ordered_client_ids"] == [second["id"], first["id"], third["id"]]
    current_second = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/notes/{second['id']}",
        headers=auth("alice"),
    )
    assert current_second.status_code == 200
    assert current_second.json()["row_version"] == pinned.json()["row_version"]

    deleted = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/items/notes/{second['id']}/delete",
        headers={**auth("alice"), "If-Match": str(pinned.json()["row_version"])},
        json={"operation_id": str(uuid.uuid4())},
    )
    assert deleted.status_code == 200, deleted.text
    after_delete = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    assert after_delete["ordered_client_ids"] == [first["id"], third["id"]]


async def test_stale_authenticated_client_rereads_one_authoritative_note_order(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    first = await _note(client, auth, tenant, project["id"], "First")
    second = await _note(client, auth, tenant, project["id"], "Second")
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/note-order"
    client_a = (await client.get(path, headers={**auth("alice"), **API_08})).json()
    client_b = dict(client_a)

    winner = await client.put(
        path, headers={**auth("alice"), **API_08},
        json=_envelope(client_a, [second["id"], first["id"]]),
    )
    assert winner.status_code == 200
    loser = await client.put(
        path, headers={**auth("alice"), **API_08},
        json=_envelope(client_b, [first["id"], second["id"]]),
    )
    assert loser.status_code == 409
    assert (await client.get(path, headers={**auth("alice"), **API_08})).json() == winner.json()


async def test_note_order_requires_explicit_api_08_negotiation(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    path = f"/v1/tenants/{tenant}/projects/{project['id']}/interaction/note-order"
    missing = await client.get(path, headers=auth("alice"))
    wrong = await client.get(
        path, headers={**auth("alice"), "X-Blocks-API-Version": "0.7"},
    )
    assert missing.status_code == wrong.status_code == 400
    assert (await client.get(path, headers={**auth("alice"), **API_08})).status_code == 200
