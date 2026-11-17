"""Opt-in Dashboard Park summary against real PostgreSQL and the page rule."""

from __future__ import annotations

import time
from uuid import UUID, uuid4

from blocks_service import project_model as pm


async def tenant(client, auth, who="alice"):
    response = await client.post("/v1/session", headers=auth(who))
    assert response.status_code == 200, response.text
    return response.json()


async def project(client, auth, tenant_id, name, who="alice"):
    response = await client.post(
        f"/v1/tenants/{tenant_id}/projects", headers=auth(who), json={"name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def park(client, auth, tenant_id, item, label, when, supersedes=None):
    response = await client.post(
        f"/v1/tenants/{tenant_id}/projects/{item['id']}/parking",
        headers=auth("alice"),
        json={
            "where_i_am": label,
            "next_physical_action": f"Next for {label}",
            "must_not_forget": f"Remember {label}",
            "occurred_at": when,
            "supersedes_id": supersedes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def summary(record):
    return {
        key: record[key]
        for key in (
            "id",
            "occurred_at",
            "where_i_am",
            "next_physical_action",
            "must_not_forget",
        )
    }


async def test_include_key_on_every_item_and_omission_unchanged(client, auth):
    tenant_id = (await tenant(client, auth))["personal_tenant_id"]
    with_park = await project(client, auth, tenant_id, "with Park")
    empty = await project(client, auth, tenant_id, "without Park")
    created = await park(
        client, auth, tenant_id, with_park, "At the desk", "2026-01-02T10:00:00Z"
    )
    path = f"/v1/tenants/{tenant_id}/projects"
    plain = await client.get(path, headers=auth("alice"))
    included = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("alice")
    )
    assert plain.status_code == included.status_code == 200
    assert [row["id"] for row in plain.json()] == [row["id"] for row in included.json()]
    assert all("newest_parking_summary" not in row for row in plain.json())
    assert all("newest_parking_summary" in row for row in included.json())
    by_id = {row["id"]: row for row in included.json()}
    assert by_id[with_park["id"]]["newest_parking_summary"] == summary(created)
    assert by_id[empty["id"]]["newest_parking_summary"] is None
    for old, new in zip(plain.json(), included.json(), strict=True):
        assert {k: v for k, v in new.items() if k != "newest_parking_summary"} == old


async def test_page_parity_after_correction_archive_and_delete(client, auth):
    tenant_id = (await tenant(client, auth))["personal_tenant_id"]
    corrected = await project(client, auth, tenant_id, "corrected")
    deleted = await project(client, auth, tenant_id, "deleted")
    original = await park(
        client, auth, tenant_id, corrected, "original", "2026-02-03T10:00:00Z"
    )
    successor = await park(
        client,
        auth,
        tenant_id,
        corrected,
        "correction",
        "2026-02-01T10:00:00Z",
        supersedes=original["id"],
    )
    to_delete = await park(
        client, auth, tenant_id, deleted, "temporary", "2026-02-04T10:00:00Z"
    )
    response = await client.post(
        f"/v1/tenants/{tenant_id}/projects/{deleted['id']}/parking/{to_delete['id']}/delete",
        headers=auth("alice"),
        json={
            "operation_id": str(uuid4()),
            "aggregate_id": to_delete["id"],
            "target_id": to_delete["id"],
            "base_version": to_delete["row_version"],
        },
    )
    assert response.status_code == 200, response.text
    path = f"/v1/tenants/{tenant_id}/projects"
    active = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("alice")
    )
    page = await client.get(f"{path}/{corrected['id']}", headers=auth("alice"))
    assert active.status_code == page.status_code == 200
    by_id = {row["id"]: row for row in active.json()}
    assert (
        by_id[corrected["id"]]["newest_parking_summary"]
        == summary(page.json()["newest_parking"])
        == summary(successor)
    )
    assert by_id[deleted["id"]]["newest_parking_summary"] is None
    archived = await client.post(
        f"{path}/{corrected['id']}/archive",
        headers={**auth("alice"), "If-Match": str(corrected["row_version"])},
    )
    assert archived.status_code == 200
    active = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("alice")
    )
    archive_list = await client.get(
        f"{path}?status=archived&include=newest_parking_summary", headers=auth("alice")
    )
    assert [row["id"] for row in active.json()] == [deleted["id"]]
    assert [row["id"] for row in archive_list.json()] == [corrected["id"]]
    assert archive_list.json()[0]["newest_parking_summary"] == summary(successor)


async def test_invalid_include_and_tenant_isolation(client, auth):
    tenant_id = (await tenant(client, auth))["personal_tenant_id"]
    foreign = (await tenant(client, auth, "bob"))["personal_tenant_id"]
    own = await project(client, auth, tenant_id, "private")
    await project(client, auth, foreign, "foreign", "bob")
    path = f"/v1/tenants/{tenant_id}/projects"
    for query in (
        "include=",
        "include=unknown",
        "include=newest_parking_summary,other",
        "include=newest_parking_summary&include=newest_parking_summary",
    ):
        response = await client.get(f"{path}?{query}", headers=auth("alice"))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    included = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("alice")
    )
    assert [row["id"] for row in included.json()] == [own["id"]]
    denied = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("bob")
    )
    assert denied.status_code == 403


async def test_summary_list_uses_one_database_statement(client, auth, db):
    identity = await tenant(client, auth)
    tenant_id, actor_id = identity["personal_tenant_id"], identity["user_id"]
    await project(client, auth, tenant_id, "one")

    class CountingConnection:
        def __init__(self, conn):
            self.conn = conn
            self.statements = []

        async def execute(self, sql, params=None):
            self.statements.append(sql)
            return await self.conn.execute(sql, params)

    async with db.tenant_transaction(UUID(tenant_id), UUID(actor_id)) as conn:
        counting = CountingConnection(conn)
        rows = await pm.list_projects(counting, include_newest_parking_summary=True)
    assert len(rows) == 1
    assert len(counting.statements) == 1
    assert "join lateral" in counting.statements[0]


async def test_twenty_project_measurement_prints_payload_plan_and_timing(
    client, auth, db
):
    identity = await tenant(client, auth)
    tenant_id, actor_id = identity["personal_tenant_id"], identity["user_id"]
    for index in range(20):
        item = await project(client, auth, tenant_id, f"Project {index:02d}")
        if index % 2 == 0:
            await park(
                client,
                auth,
                tenant_id,
                item,
                f"Park {index:02d}",
                "2026-03-01T10:00:00Z",
            )
    path = f"/v1/tenants/{tenant_id}/projects"
    before = time.perf_counter()
    plain = await client.get(path, headers=auth("alice"))
    details = [
        await client.get(f"{path}/{item['id']}", headers=auth("alice"))
        for item in plain.json()
    ]
    baseline_ms = (time.perf_counter() - before) * 1000
    before = time.perf_counter()
    included = await client.get(
        f"{path}?include=newest_parking_summary", headers=auth("alice")
    )
    projected_ms = (time.perf_counter() - before) * 1000
    assert len(plain.json()) == len(included.json()) == len(details) == 20
    assert all(response.status_code == 200 for response in details)
    assert included.status_code == 200
    async with db.tenant_transaction(UUID(tenant_id), UUID(actor_id)) as conn:

        class Capture:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.statement = None

            async def execute(self, sql, params=None):
                self.statement = sql
                return await self.wrapped.execute(sql, params)

        captured = Capture(conn)
        await pm.list_projects(captured, include_newest_parking_summary=True)
        plan_cur = await conn.execute(
            "explain (analyze, buffers, format json) " + captured.statement
        )
        plan = (await plan_cur.fetchone())["QUERY PLAN"][0]
    print(
        "20-project Park read: baseline 21 requests / "
        f"{len(plain.content) + sum(len(item.content) for item in details)} bytes / "
        f"{baseline_ms:.2f} ms; projected 1 request / {len(included.content)} bytes / "
        f"{projected_ms:.2f} ms; SQL root {plan['Plan']['Node Type']} / "
        f"{plan['Execution Time']:.2f} ms"
    )
