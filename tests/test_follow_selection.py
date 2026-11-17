"""Proposed Follow arbitration against migrated PostgreSQL and forced actor RLS."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID, uuid4


async def _start(client, auth, who="alice") -> tuple[str, str]:
    response = await client.post("/v1/session", headers=auth(who))
    assert response.status_code == 200, response.text
    body = response.json()
    return body["personal_tenant_id"], body["user_id"]


async def _project(client, auth, tenant: str, who="alice") -> dict:
    response = await client.post(
        f"/v1/tenants/{tenant}/projects",
        headers=auth(who),
        json={"name": "Follow test project"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _intent(
    project: str,
    sequence: int,
    *,
    kind="explicit_button",
    dispatch="immediate",
    observed=None,
    age=0,
) -> dict:
    return {
        "selection_sequence": sequence,
        "project_id": project,
        "intent_kind": kind,
        "dispatch_class": dispatch,
        "observed_revision": observed,
        "age_ms": age,
    }


async def _put(client, auth, tenant: str, source: UUID, body: dict, who="alice"):
    return await client.put(
        f"/v1/tenants/{tenant}/follow/selection/{source}",
        headers=auth(who),
        json=body,
    )


async def _get(client, auth, tenant: str, who="alice", source: UUID | None = None):
    query = f"?automatic_source_device_id={source}" if source else ""
    return await client.get(
        f"/v1/tenants/{tenant}/follow/selection{query}",
        headers=auth(who),
    )


async def test_first_online_press_without_baseline_and_lost_reply_replay(
    client, auth, db
):
    tenant, actor_id = await _start(client, auth)
    project = await _project(client, auth, tenant)
    source = uuid4()
    async with db.tenant_transaction(UUID(tenant), UUID(actor_id)) as conn:
        cur = await conn.execute("select count(*) as n from blocks.outbox")
        outbox_before = (await cur.fetchone())["n"]
    before = await _get(client, auth, tenant)
    assert before.json() == {"current_revision": 0, "selection": None}

    first = await _put(
        client, auth, tenant, source, _intent(project["id"], 1, age=9_999)
    )
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "private, no-store"
    assert first.json()["revision"] == 1
    assert first.json()["replayed"] is False
    assert first.json()["stream_status"] == "current"
    start = datetime.fromisoformat(first.json()["accepted_at"])
    expiry = datetime.fromisoformat(first.json()["expires_at"])
    assert (expiry - start).total_seconds() == 50.001

    # The attempt's age may increase past both admission bounds. Replay lookup
    # precedes age checks and returns the original receipt/expiry unchanged.
    replay = await _put(
        client, auth, tenant, source, _intent(project["id"], 1, age=60_001)
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert {k: replay.json()[k] for k in ("revision", "accepted_at", "expires_at")} == {
        k: first.json()[k] for k in ("revision", "accepted_at", "expires_at")
    }
    for _ in range(2):
        snapshot = await _get(client, auth, tenant)
        assert snapshot.json()["current_revision"] == 1
        assert snapshot.json()["selection"]["project_id"] == project["id"]
        assert snapshot.headers["Cache-Control"] == "private, no-store"
    page = await client.get(
        f"/v1/tenants/{tenant}/projects/{project['id']}", headers=auth("alice")
    )
    assert page.json()["project"]["row_version"] == project["row_version"]
    async with db.tenant_transaction(UUID(tenant), UUID(actor_id)) as conn:
        cur = await conn.execute("select count(*) as n from blocks.outbox")
        assert (await cur.fetchone())["n"] == outbox_before


async def test_unsent_latest_sequence_and_late_a_cannot_roll_back(client, auth):
    tenant, _ = await _start(client, auth)
    a = await _project(client, auth, tenant)
    b = await _project(client, auth, tenant)
    source = uuid4()
    # Android coalesces unsent A; the Service sees B first.
    accepted_b = await _put(client, auth, tenant, source, _intent(b["id"], 2))
    assert accepted_b.status_code == 200
    late_a = await _put(client, auth, tenant, source, _intent(a["id"], 1))
    assert late_a.status_code == 409
    error = late_a.json()["error"]
    assert error["code"] == "conflict" and error["reason"] == "stale_sequence"
    assert "delivery" not in error, (
        "A might have been accepted before B in another schedule"
    )
    assert (await _get(client, auth, tenant)).json()["selection"]["project_id"] == b[
        "id"
    ]


async def test_a_then_b_allows_transient_a_but_final_b_and_replay_is_old(client, auth):
    tenant, _ = await _start(client, auth)
    a = await _project(client, auth, tenant)
    b = await _project(client, auth, tenant)
    source = uuid4()
    first_a = await _put(client, auth, tenant, source, _intent(a["id"], 1))
    assert first_a.status_code == 200
    assert (await _get(client, auth, tenant)).json()["selection"]["project_id"] == a[
        "id"
    ]
    accepted_b = await _put(client, auth, tenant, source, _intent(b["id"], 2))
    assert accepted_b.status_code == 200
    assert (await _get(client, auth, tenant)).json()["selection"]["project_id"] == b[
        "id"
    ]
    old_retry = await _put(
        client, auth, tenant, source, _intent(a["id"], 1, age=50_000)
    )
    assert old_retry.status_code == 409
    assert old_retry.json()["error"]["reason"] == "stale_sequence"
    assert "delivery" not in old_retry.json()["error"]


async def test_deferred_chains_only_across_uninterrupted_same_source_run(client, auth):
    tenant, _ = await _start(client, auth)
    a = await _project(client, auth, tenant)
    b = await _project(client, auth, tenant)
    source = uuid4()
    assert (
        await _put(client, auth, tenant, source, _intent(a["id"], 1))
    ).status_code == 200
    chained = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(b["id"], 2, dispatch="deferred", observed=0, age=1_000),
    )
    assert chained.status_code == 200, chained.text
    assert chained.json()["revision"] == 2

    other = uuid4()
    assert (
        await _put(client, auth, tenant, other, _intent(a["id"], 1))
    ).status_code == 200
    # A newer action from the original source must not make its earlier B
    # baseline valid again after another source intervened.
    assert (
        await _put(client, auth, tenant, source, _intent(a["id"], 3))
    ).status_code == 200
    stale = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(b["id"], 4, dispatch="deferred", observed=2, age=2_000),
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "conflict",
        "message": "The selection changed after this action.",
        "reason": "stale_candidate",
        "retryable": False,
        "delivery": "not_accepted",
        "current_revision": 4,
    }
    repeat = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(b["id"], 4, dispatch="deferred", observed=2, age=3_000),
    )
    assert (
        repeat.status_code == 409
        and repeat.json()["error"]["reason"] == "stale_candidate"
    )
    assert (await _get(client, auth, tenant)).json()["current_revision"] == 4


async def test_age_boundaries_and_immutable_conflict(client, auth):
    tenant, _ = await _start(client, auth)
    project = await _project(client, auth, tenant)
    source = uuid4()
    expired_online = await _put(
        client, auth, tenant, source, _intent(project["id"], 1, age=10_000)
    )
    assert expired_online.status_code == 400
    assert expired_online.json()["error"]["code"] == "invalid_request"
    assert expired_online.json()["error"]["reason"] == "intent_expired"
    assert expired_online.json()["error"]["delivery"] == "not_accepted"
    accepted = await _put(
        client, auth, tenant, source, _intent(project["id"], 1, age=9_999)
    )
    assert accepted.status_code == 200, (
        "rejected attempts have no accepted receipt/high-water"
    )
    changed = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(project["id"], 1, kind="automatic_nfc", age=9_999),
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["reason"] == "intent_conflict"
    assert "delivery" not in changed.json()["error"]
    expired_deferred = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(project["id"], 2, dispatch="deferred", observed=1, age=60_000),
    )
    assert expired_deferred.status_code == 400
    assert expired_deferred.json()["error"]["reason"] == "intent_expired"
    just_fresh = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(project["id"], 2, dispatch="deferred", observed=1, age=59_999),
    )
    assert just_fresh.status_code == 200
    start = datetime.fromisoformat(just_fresh.json()["accepted_at"])
    end = datetime.fromisoformat(just_fresh.json()["expires_at"])
    assert (end - start).total_seconds() == 0.001
    await asyncio.sleep(0.02)
    assert (await _get(client, auth, tenant)).json() == {
        "current_revision": 2,
        "selection": None,
    }
    late_replay = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(project["id"], 2, dispatch="deferred", observed=1, age=60_001),
    )
    assert late_replay.status_code == 200
    assert late_replay.json()["replayed"] is True
    assert late_replay.json()["stream_status"] == "expired"
    assert late_replay.json()["expires_at"] == just_fresh.json()["expires_at"]


async def test_explicit_reaches_all_readers_and_auto_needs_preference(client, auth):
    tenant, _ = await _start(client, auth)
    a = await _project(client, auth, tenant)
    b = await _project(client, auth, tenant)
    button_source, nfc_source = uuid4(), uuid4()
    assert (
        await _put(client, auth, tenant, button_source, _intent(a["id"], 1))
    ).status_code == 200
    assert (
        await _put(
            client,
            auth,
            tenant,
            nfc_source,
            _intent(b["id"], 1, kind="automatic_nfc"),
        )
    ).status_code == 200
    for _ in range(2):
        assert (await _get(client, auth, tenant)).json()["selection"][
            "project_id"
        ] == a["id"]
        preferred = await _get(client, auth, tenant, source=nfc_source)
        assert preferred.json()["selection"]["project_id"] == b["id"]
        assert preferred.json()["selection"]["intent_kind"] == "automatic_nfc"
    later_button = await _put(client, auth, tenant, button_source, _intent(a["id"], 2))
    assert later_button.status_code == 200
    assert (await _get(client, auth, tenant, source=nfc_source)).json()["selection"][
        "project_id"
    ] == a["id"]


async def test_inaccessible_latest_never_falls_back_to_older_auto(client, auth):
    tenant, _ = await _start(client, auth)
    older = await _project(client, auth, tenant)
    newer = await _project(client, auth, tenant)
    auto, button = uuid4(), uuid4()
    assert (
        await _put(
            client,
            auth,
            tenant,
            auto,
            _intent(older["id"], 1, kind="automatic_nfc"),
        )
    ).status_code == 200
    assert (
        await _put(client, auth, tenant, button, _intent(newer["id"], 1))
    ).status_code == 200
    archived = await client.post(
        f"/v1/tenants/{tenant}/projects/{newer['id']}/archive",
        headers={**auth("alice"), "If-Match": str(newer["row_version"])},
    )
    assert archived.status_code == 200
    snapshot = await _get(client, auth, tenant, source=auto)
    assert snapshot.json() == {"current_revision": 2, "selection": None}
    assert newer["id"] not in snapshot.text and older["id"] not in snapshot.text


async def test_expired_newest_never_falls_back_to_older_explicit(client, auth):
    tenant, _ = await _start(client, auth)
    older = await _project(client, auth, tenant)
    newer = await _project(client, auth, tenant)
    button, auto = uuid4(), uuid4()
    assert (
        await _put(client, auth, tenant, button, _intent(older["id"], 1))
    ).status_code == 200
    baseline = (await _get(client, auth, tenant)).json()["current_revision"]
    assert (
        await _put(
            client,
            auth,
            tenant,
            auto,
            _intent(
                newer["id"],
                1,
                kind="automatic_nfc",
                dispatch="deferred",
                observed=baseline,
                age=59_999,
            ),
        )
    ).status_code == 200
    await asyncio.sleep(0.02)
    snapshot = await _get(client, auth, tenant, source=auto)
    assert snapshot.json() == {"current_revision": 2, "selection": None}
    assert older["id"] not in snapshot.text and newer["id"] not in snapshot.text


async def test_deleted_newest_keeps_revision_and_blocks_older_fallback(
    client, auth, db
):
    tenant, actor_id = await _start(client, auth)
    older = await _project(client, auth, tenant)
    newer = await _project(client, auth, tenant)
    auto, button = uuid4(), uuid4()
    assert (
        await _put(
            client,
            auth,
            tenant,
            auto,
            _intent(older["id"], 1, kind="automatic_nfc"),
        )
    ).status_code == 200
    assert (
        await _put(client, auth, tenant, button, _intent(newer["id"], 1))
    ).status_code == 200
    async with db.tenant_transaction(UUID(tenant), UUID(actor_id)) as conn:
        await conn.execute(
            "delete from blocks.projects where tenant_id = %s and id = %s",
            (tenant, newer["id"]),
        )
    snapshot = await _get(client, auth, tenant, source=auto)
    assert snapshot.json() == {"current_revision": 2, "selection": None}
    assert older["id"] not in snapshot.text and newer["id"] not in snapshot.text


async def test_actor_tenant_rls_and_archived_target_do_not_leak(client, auth, db):
    tenant, alice_id = await _start(client, auth)
    other_tenant, bob_id = await _start(client, auth, "bob")
    project = await _project(client, auth, tenant)
    source = uuid4()
    assert (
        await _put(client, auth, tenant, source, _intent(project["id"], 1))
    ).status_code == 200
    no_token = await client.get(f"/v1/tenants/{tenant}/follow/selection")
    assert no_token.status_code == 401
    assert no_token.headers["Cache-Control"] == "private, no-store"
    assert "revision" not in no_token.text and project["id"] not in no_token.text
    denied = await _get(client, auth, tenant, who="bob")
    assert denied.status_code == 403 and "revision" not in denied.text
    cross_target = await _put(
        client, auth, other_tenant, uuid4(), _intent(project["id"], 1), "bob"
    )
    assert cross_target.status_code == 404 and project["id"] not in cross_target.text
    async with db.transaction() as conn:
        cur = await conn.execute("select count(*) as n from blocks.follow_sources")
        assert (await cur.fetchone())["n"] == 0, "no context must see no Follow rows"
    async with db.tenant_transaction(UUID(tenant), UUID(alice_id)) as conn:
        await conn.execute(
            "insert into blocks.memberships (tenant_id, user_id, role)"
            " values (%s, %s, 'member')",
            (tenant, bob_id),
        )
    bob_read = await _get(client, auth, tenant, who="bob")
    assert bob_read.status_code == 200
    assert bob_read.json() == {"current_revision": 0, "selection": None}
    async with db.tenant_transaction(UUID(tenant), UUID(alice_id)) as conn:
        await conn.execute(
            "delete from blocks.memberships where tenant_id = %s and user_id = %s",
            (tenant, bob_id),
        )
    assert (await _get(client, auth, tenant, who="bob")).status_code == 403
    async with db.tenant_transaction(UUID(tenant), UUID(alice_id)) as conn:
        await conn.execute(
            "insert into blocks.memberships (tenant_id, user_id, role)"
            " values (%s, %s, 'member')",
            (tenant, bob_id),
        )
    assert (await _get(client, auth, tenant, who="bob")).json()["current_revision"] == 0
    archived = await client.post(
        f"/v1/tenants/{tenant}/projects/{project['id']}/archive",
        headers={**auth("alice"), "If-Match": str(project["row_version"])},
    )
    assert archived.status_code == 200
    unreadable = await _get(client, auth, tenant)
    assert unreadable.status_code == 200
    assert unreadable.json() == {"current_revision": 1, "selection": None}
    assert project["id"] not in unreadable.text


async def test_deferred_requires_action_time_baseline(client, auth):
    tenant, _ = await _start(client, auth)
    project = await _project(client, auth, tenant)
    source = uuid4()
    invalid = await _put(
        client,
        auth,
        tenant,
        source,
        _intent(project["id"], 1, dispatch="deferred", observed=None),
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert (await _get(client, auth, tenant)).json()["current_revision"] == 0


async def test_source_cap_is_terminal_and_does_not_prune_highwater(client, auth, db):
    tenant, actor_id = await _start(client, auth)
    project = await _project(client, auth, tenant)
    sources = [uuid4() for _ in range(33)]
    for source in sources[:32]:
        accepted = await _put(client, auth, tenant, source, _intent(project["id"], 1))
        assert accepted.status_code == 200, accepted.text
    over = await _put(client, auth, tenant, sources[32], _intent(project["id"], 1))
    assert over.status_code == 409
    assert over.json()["error"]["reason"] == "source_limit_reached"
    assert over.json()["error"]["delivery"] == "not_accepted"
    async with db.tenant_transaction(UUID(tenant), UUID(actor_id)) as conn:
        cur = await conn.execute("select count(*) as n from blocks.follow_sources")
        assert (await cur.fetchone())["n"] == 32
    assert (await _get(client, auth, tenant)).json()["current_revision"] == 32
