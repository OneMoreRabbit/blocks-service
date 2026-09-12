"""Client-generated block identity and tag tokens (ADR-0014, API 0.5).

The premise is the one ADR-0010 and ADR-0011 established, applied to hardware: an
Android phone programs a physical tag while offline, so by the time Service hears
about it the token is already written to something the user is holding. Service
therefore accepts the client's token rather than issuing a competing one, and
identity has to carry convergence because the idempotency result may be long gone.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid

import psycopg
import pytest

from blocks_service.db import Database


def canonical_token() -> str:
    """32 random bytes, 43 characters of unpadded base64url — exactly ADR-0014."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _register(client, auth, tenant, who="alice", **body):
    return await client.post(
        f"/v1/tenants/{tenant}/blocks", headers=auth(who), json=body
    )


async def _expire_results(db: Database, tenant: str, actor: str) -> int:
    """The ordinary idempotency-result record ageing out.

    Retention is a declared minimum, not a maximum. A client returning after
    longer than the window finds the result gone — which is the case client
    identity exists for, and the only way to test it is to delete the record.
    """
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("delete from blocks.idempotency_keys")
        return cur.rowcount


async def _actor(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["user_id"]


# --------------------------------------------------------------------------
# The 0.4 flow is untouched
# --------------------------------------------------------------------------


async def test_a_0_4_registration_still_mints_its_own_token(client, auth):
    tenant = await _tenant(client, auth)
    r = await _register(client, auth, tenant, label="Workbench")

    assert r.status_code == 201
    assert "client_id" not in r.json(), "the 0.4 result shape is unchanged"
    assert len(r.json()["tag_token"]) == 43, "the minted token is canonical too"


async def test_half_an_identity_is_refused(client, auth):
    """A client_id without a token cannot replay and cannot conflict."""
    tenant = await _tenant(client, auth)

    only_id = await _register(client, auth, tenant, label="B", client_id=str(uuid.uuid4()))
    only_token = await _register(client, auth, tenant, label="B", tag_token=canonical_token())

    assert only_id.status_code == 400, "this service reports validation as 400, not 422"
    assert only_token.status_code == 400


# --------------------------------------------------------------------------
# Canonical encoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad, why",
    [
        ("short", "wrong length"),
        ("A" * 44, "wrong length"),
        (base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), "padded"),
        (base64.b64encode(b"\xff" * 32).rstrip(b"=").decode(), "standard alphabet"),
        ("blocks://tag/v1/" + canonical_token(), "the whole URI, not the token"),
        ("!" * 43, "not base64url at all"),
    ],
)
async def test_a_non_canonical_token_is_refused(client, auth, bad, why):
    """Two spellings of one secret would hash differently and register twice.

    So the encoding is canonical, not merely decodable: the same physical tag must
    never be able to become two blocks.
    """
    tenant = await _tenant(client, auth)
    r = await _register(
        client, auth, tenant, label="B", client_id=str(uuid.uuid4()), tag_token=bad
    )
    assert r.status_code == 400, why
    assert r.json()["error"]["code"] == "invalid_request", why


# --------------------------------------------------------------------------
# Replay by client identity, before and after the result expires
# --------------------------------------------------------------------------


async def test_an_identical_registration_replays_the_retained_result(client, auth):
    tenant = await _tenant(client, auth)
    body = {"label": "Workbench", "client_id": str(uuid.uuid4()), "tag_token": canonical_token()}
    headers = {**auth("alice"), "Idempotency-Key": "reg-1"}
    path = f"/v1/tenants/{tenant}/blocks"

    first = await client.post(path, headers=headers, json=body)
    second = await client.post(path, headers=headers, json=body)

    assert first.status_code == 201
    assert first.json()["client_id"] == body["client_id"]
    assert "tag_token" not in first.json(), (
        "the client already holds the token; echoing it would put the raw value "
        "into the idempotency result, which is persisted as JSON"
    )
    assert first.json()["id"] != body["client_id"], "canonical id stays server-generated"
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"


async def test_it_replays_by_resource_identity_after_the_result_expires(
    client, auth, db: Database
):
    """The case the design exists for: the phone returns after the window."""
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    body = {"label": "Workbench", "client_id": str(uuid.uuid4()), "tag_token": canonical_token()}
    headers = {**auth("alice"), "Idempotency-Key": "reg-1"}
    path = f"/v1/tenants/{tenant}/blocks"

    first = await client.post(path, headers=headers, json=body)
    assert first.status_code == 201
    assert await _expire_results(db, tenant, actor) >= 1

    replay = await client.post(path, headers=headers, json=body)
    assert replay.status_code == 200, "nothing was created, so not 201"
    assert replay.json()["id"] == first.json()["id"], "the same canonical block"
    assert replay.headers.get("Idempotent-Replay-Source") == "resource"
    assert "tag_token" not in replay.json(), (
        "a resource replay is a read of current state; the token is not stored "
        "and the client that supplied it already has it"
    )

    listed = await client.get(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"))
    assert len(listed.json()) == 1, "exactly one block, no duplicate"


@pytest.mark.parametrize("changed", ["label", "tag_token"])
async def test_the_same_identity_with_a_different_operation_conflicts(
    client, auth, db: Database, changed
):
    """First registration wins; never last-write-wins."""
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    cid = str(uuid.uuid4())
    body = {"label": "Workbench", "client_id": cid, "tag_token": canonical_token()}
    path = f"/v1/tenants/{tenant}/blocks"

    first = await client.post(path, headers={**auth("alice"), "Idempotency-Key": "a"}, json=body)
    assert first.status_code == 201
    await _expire_results(db, tenant, actor)

    altered = dict(body)
    altered[changed] = "Different" if changed == "label" else canonical_token()
    clash = await client.post(
        path, headers={**auth("alice"), "Idempotency-Key": "b"}, json=altered
    )

    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"
    listed = await client.get(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"))
    assert len(listed.json()) == 1, "the conflict committed nothing"
    assert listed.json()[0]["label"] == "Workbench", "first registration wins"


# --------------------------------------------------------------------------
# Token collision, within a tenant and across tenants
# --------------------------------------------------------------------------


async def test_the_same_token_on_a_second_block_conflicts_without_saying_why(
    client, auth
):
    tenant = await _tenant(client, auth)
    token = canonical_token()
    path = f"/v1/tenants/{tenant}/blocks"

    first = await client.post(path, headers=auth("alice"), json={
        "label": "One", "client_id": str(uuid.uuid4()), "tag_token": token})
    second = await client.post(path, headers=auth("alice"), json={
        "label": "Two", "client_id": str(uuid.uuid4()), "tag_token": token})

    assert first.status_code == 201
    assert second.status_code == 409
    body = second.json()["error"]
    assert body["code"] == "conflict"
    assert token not in str(second.json()), "the raw token is never echoed in an error"
    assert "client_id" not in body["message"] and "token" not in body["message"], (
        "the caller is not told WHICH of its two values collided"
    )


async def test_the_same_token_in_another_tenant_is_independent(client, auth):
    """Tenant-scoped uniqueness, for the reason ADR-0011 gave: a global index is an oracle."""
    alice = await _tenant(client, auth, "alice")
    bob = await _tenant(client, auth, "bob")
    token = canonical_token()

    mine = await client.post(f"/v1/tenants/{alice}/blocks", headers=auth("alice"), json={
        "label": "Mine", "client_id": str(uuid.uuid4()), "tag_token": token})
    theirs = await client.post(f"/v1/tenants/{bob}/blocks", headers=auth("bob"), json={
        "label": "Theirs", "client_id": str(uuid.uuid4()), "tag_token": token})

    assert mine.status_code == 201
    assert theirs.status_code == 201, (
        "the same raw value in another tenant neither collides nor reveals existence"
    )
    assert mine.json()["id"] != theirs.json()["id"]


async def test_a_tenant_cannot_resolve_another_tenants_tag(client, auth):
    alice = await _tenant(client, auth, "alice")
    bob = await _tenant(client, auth, "bob")
    token = canonical_token()
    await client.post(f"/v1/tenants/{alice}/blocks", headers=auth("alice"), json={
        "label": "Mine", "client_id": str(uuid.uuid4()), "tag_token": token})

    seen = await client.post(
        f"/v1/tenants/{bob}/blocks/lookup", headers=auth("bob"), json={"tag_token": token}
    )
    assert seen.status_code in (403, 404), "indistinguishable from a token that never existed"


async def test_the_same_client_id_in_another_tenant_is_independent(client, auth):
    alice = await _tenant(client, auth, "alice")
    bob = await _tenant(client, auth, "bob")
    cid = str(uuid.uuid4())

    mine = await client.post(f"/v1/tenants/{alice}/blocks", headers=auth("alice"), json={
        "label": "Mine", "client_id": cid, "tag_token": canonical_token()})
    theirs = await client.post(f"/v1/tenants/{bob}/blocks", headers=auth("bob"), json={
        "label": "Theirs", "client_id": cid, "tag_token": canonical_token()})

    assert mine.status_code == 201
    assert theirs.status_code == 201


# --------------------------------------------------------------------------
# The raw token does not survive the request
# --------------------------------------------------------------------------


async def test_the_raw_token_is_nowhere_but_the_registration_result(
    client, auth, db: Database
):
    """Storage, events and reads. The hash is the only durable form."""
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    token = canonical_token()
    registered = await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={
        "label": "Workbench", "client_id": str(uuid.uuid4()), "tag_token": token})
    assert registered.status_code == 201
    block = registered.json()["id"]

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select id::text, label, client_id::text, encode(token_hash,'hex') as h,"
            " encode(creation_fingerprint,'hex') as f from blocks.physical_blocks"
        )
        rows = await cur.fetchall()
        cur = await conn.execute("select event_type, payload::text from blocks.outbox")
        events = await cur.fetchall()

    assert token not in str(rows), "no raw token in the domain table"
    assert token not in str(events), "no raw token in any event payload"
    assert rows[0]["f"], "the creation fingerprint is retained"

    listed = await client.get(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"))
    assert token not in str(listed.json()), "block listings never carry the token"

    page = await client.get(f"/v1/tenants/{tenant}/blocks/{block}", headers=auth("alice"))
    if page.status_code == 200:
        assert token not in str(page.json()), "a block read never carries the token"


async def test_the_fingerprint_is_over_the_hash_not_the_raw_token(client, auth, db: Database):
    """Retaining a fingerprint is only safe if it cannot be reversed to the token."""
    from blocks_service.operating_loop import block_creation_fingerprint, hash_token

    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    cid, token = uuid.uuid4(), canonical_token()
    await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={
        "label": "Workbench", "client_id": str(cid), "tag_token": token})

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select creation_fingerprint from blocks.physical_blocks")
        stored = bytes((await cur.fetchone())["creation_fingerprint"])

    assert stored == block_creation_fingerprint(cid, "Workbench", hash_token(token))


# --------------------------------------------------------------------------
# Ordered register-then-bind, and equivalence with manual browsing
# --------------------------------------------------------------------------


async def test_binding_uses_the_canonical_id_returned_by_registration(client, auth):
    """Android sends binding only after registration maps its client id."""
    tenant = await _tenant(client, auth)
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "Kitchen"}
    )).json()["id"]
    cid, token = str(uuid.uuid4()), canonical_token()

    registered = await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={
        "label": "Workbench", "client_id": cid, "tag_token": token})
    canonical = registered.json()["id"]
    assert canonical != cid

    bound = await client.post(
        f"/v1/tenants/{tenant}/blocks/{canonical}/binding",
        headers=auth("alice"), json={"project_id": project},
    )
    assert bound.status_code in (200, 201)

    resolved = await client.post(
        f"/v1/tenants/{tenant}/blocks/lookup", headers=auth("alice"), json={"tag_token": token}
    )
    assert resolved.status_code == 200
    assert resolved.json()["block_id"] == canonical
    assert resolved.json()["project_id"] == project, (
        "the tag opens the same project manual browsing would"
    )


async def test_a_client_identity_is_not_a_service_address(client, auth):
    """The client id names the row for its owner; it never addresses the resource.

    Deliberately checked through a read rather than a bind: binding to an
    unregistered id is a separate pre-existing fault (an unhandled foreign-key
    violation on API 0.4's bind path), reported to architecture rather than
    changed here, because this need requires existing bind behaviour to remain
    intact.
    """
    tenant = await _tenant(client, auth)
    cid, token = str(uuid.uuid4()), canonical_token()
    registered = await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={
        "label": "Workbench", "client_id": cid, "tag_token": token})
    canonical = registered.json()["id"]
    assert canonical != cid, "the canonical id is server-generated"

    by_client_id = await client.get(
        f"/v1/tenants/{tenant}/blocks/{cid}", headers=auth("alice")
    )
    assert by_client_id.status_code in (403, 404), "no block answers to a client identity"


# --------------------------------------------------------------------------
# Concurrency: two deliveries of one registration
# --------------------------------------------------------------------------


async def test_simultaneous_identical_registrations_make_one_block(client, auth, contested):
    arrived = contested(2)
    tenant = await _tenant(client, auth)
    headers = {**auth("alice"), "Idempotency-Key": "race-reg"}
    body = {"label": "Workbench", "client_id": str(uuid.uuid4()), "tag_token": canonical_token()}
    path = f"/v1/tenants/{tenant}/blocks"

    responses = await asyncio.gather(
        client.post(path, headers=headers, json=body),
        client.post(path, headers=headers, json=body),
        return_exceptions=True,
    )
    assert len(arrived) == 2, "both callers must have reached the contested decision"
    assert 409 not in [r.status_code for r in responses], "a retry must not conflict with itself"
    listed = await client.get(path, headers=auth("alice"))
    assert len(listed.json()) == 1, "exactly one block"


async def test_simultaneous_registrations_sharing_a_token_leave_one_block(
    client, auth, contested
):
    arrived = contested(2)
    tenant = await _tenant(client, auth)
    token = canonical_token()
    path = f"/v1/tenants/{tenant}/blocks"

    # Distinct keys: these are two different operations that happen to collide on
    # the token, not one operation delivered twice. A key each is also what puts
    # both callers through the contested missing-key read the barrier holds.
    responses = await asyncio.gather(
        client.post(path, headers={**auth("alice"), "Idempotency-Key": "tok-a"}, json={
            "label": "One", "client_id": str(uuid.uuid4()), "tag_token": token}),
        client.post(path, headers={**auth("alice"), "Idempotency-Key": "tok-b"}, json={
            "label": "Two", "client_id": str(uuid.uuid4()), "tag_token": token}),
        return_exceptions=True,
    )
    assert len(arrived) == 2
    assert sorted(r.status_code for r in responses) == [201, 409]
    listed = await client.get(path, headers=auth("alice"))
    assert len(listed.json()) == 1, "the loser committed no block"


# --------------------------------------------------------------------------
# Binding fails closed, at both layers
# --------------------------------------------------------------------------


async def _block_and_project(client, auth, tenant, who="alice"):
    block = (await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth(who), json={
        "label": "Workbench"})).json()["id"]
    project = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth(who), json={"name": "Kitchen"}
    )).json()["id"]
    return block, project


@pytest.mark.parametrize("missing", ["block", "project"])
async def test_binding_an_absent_id_is_404_and_commits_nothing(
    client, auth, db: Database, missing
):
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    block, project = await _block_and_project(client, auth, tenant)
    if missing == "block":
        block = str(uuid.uuid4())
    else:
        project = str(uuid.uuid4())

    r = await client.post(
        f"/v1/tenants/{tenant}/blocks/{block}/binding",
        headers={**auth("alice"), "Idempotency-Key": "bind-miss"},
        json={"project_id": project},
    )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute("select count(*) as n from blocks.block_bindings")
        assert (await cur.fetchone())["n"] == 0, "no binding row"
        cur = await conn.execute(
            "select count(*) as n from blocks.outbox where event_type = 'block.bound'")
        assert (await cur.fetchone())["n"] == 0, "no outbox effect"
        cur = await conn.execute("select count(*) as n from blocks.idempotency_keys")
        assert (await cur.fetchone())["n"] == 0, "no idempotency result retained"


@pytest.mark.parametrize("foreign", ["block", "project"])
async def test_binding_a_foreign_id_answers_exactly_as_an_absent_one(
    client, auth, db: Database, foreign
):
    """The distinction between 'never existed' and 'belongs to someone else' IS the oracle."""
    mine = await _tenant(client, auth, "alice")
    actor = await _actor(client, auth, "alice")
    theirs = await _tenant(client, auth, "bob")
    block, project = await _block_and_project(client, auth, mine)
    their_block, their_project = await _block_and_project(client, auth, theirs, "bob")
    if foreign == "block":
        block = their_block
    else:
        project = their_project

    r = await client.post(
        f"/v1/tenants/{mine}/blocks/{block}/binding",
        headers=auth("alice"), json={"project_id": project},
    )
    absent = await client.post(
        f"/v1/tenants/{mine}/blocks/{uuid.uuid4() if foreign == 'block' else block}/binding",
        headers=auth("alice"),
        json={"project_id": str(uuid.uuid4()) if foreign == "project" else project},
    )

    assert r.status_code == 404
    assert r.json() == absent.json(), "foreign and absent are one indistinguishable answer"

    async with db.tenant_transaction(mine, actor) as conn:
        cur = await conn.execute("select count(*) as n from blocks.block_bindings")
        assert (await cur.fetchone())["n"] == 0


async def test_a_foreign_bind_does_not_end_an_existing_binding(client, auth, db: Database):
    """The old order ended the current binding BEFORE it failed."""
    mine = await _tenant(client, auth, "alice")
    actor = await _actor(client, auth, "alice")
    theirs = await _tenant(client, auth, "bob")
    block, project = await _block_and_project(client, auth, mine)
    _, their_project = await _block_and_project(client, auth, theirs, "bob")

    ok = await client.post(f"/v1/tenants/{mine}/blocks/{block}/binding",
                           headers=auth("alice"), json={"project_id": project})
    assert ok.status_code in (200, 201)

    bad = await client.post(f"/v1/tenants/{mine}/blocks/{block}/binding",
                            headers=auth("alice"), json={"project_id": their_project})
    assert bad.status_code == 404

    async with db.tenant_transaction(mine, actor) as conn:
        cur = await conn.execute(
            "select count(*) as n from blocks.block_bindings where is_current")
        assert (await cur.fetchone())["n"] == 1, "the real binding survived"


async def test_the_database_refuses_a_cross_tenant_binding_directly(
    client, auth, db: Database
):
    """The half that holds when application validation is bypassed."""
    mine = await _tenant(client, auth, "alice")
    actor = await _actor(client, auth, "alice")
    theirs = await _tenant(client, auth, "bob")
    block, _ = await _block_and_project(client, auth, mine)
    _, their_project = await _block_and_project(client, auth, theirs, "bob")

    async with db.tenant_transaction(mine, actor) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute(
                    "insert into blocks.block_bindings (tenant_id, block_id, project_id)"
                    " values (%s, %s, %s)",
                    (mine, block, their_project),
                )


async def test_valid_bind_rebind_and_unbind_still_work(client, auth, db: Database):
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    block, first = await _block_and_project(client, auth, tenant)
    second = (await client.post(
        f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={"name": "Loft"}
    )).json()["id"]

    a = await client.post(f"/v1/tenants/{tenant}/blocks/{block}/binding",
                          headers=auth("alice"), json={"project_id": first})
    b = await client.post(f"/v1/tenants/{tenant}/blocks/{block}/binding",
                          headers=auth("alice"), json={"project_id": second})
    assert a.status_code in (200, 201) and b.status_code in (200, 201)

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select count(*) as n from blocks.block_bindings where block_id = %s", (block,))
        assert (await cur.fetchone())["n"] == 2, "rebinding appends; history is kept"
        cur = await conn.execute(
            "select project_id::text as p from blocks.block_bindings"
            " where block_id = %s and is_current", (block,))
        rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0]["p"] == second, "exactly one current binding"


# --------------------------------------------------------------------------
# The raw token really is absent from persistent storage
# --------------------------------------------------------------------------


async def test_the_raw_token_never_reaches_the_idempotency_record(
    client, auth, db: Database
):
    """The claim the first draft got wrong.

    A response is persisted as JSON by the idempotency machinery, so echoing the
    token would have written the raw value into Service storage — while the
    contract said storage held only hashes. Not a display decision.
    """
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    token = canonical_token()
    r = await client.post(f"/v1/tenants/{tenant}/blocks",
                          headers={**auth("alice"), "Idempotency-Key": "store"},
                          json={"label": "Workbench", "client_id": str(uuid.uuid4()),
                                "tag_token": token})
    assert r.status_code == 201

    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select status_code, response::text as body,"
            " encode(fingerprint,'hex') as fp from blocks.idempotency_keys")
        stored = await cur.fetchall()

    assert stored, "the result was retained"
    assert token not in str(stored), "no raw token in the idempotency response or fingerprint"


async def test_a_client_form_replay_after_expiry_carries_no_token(
    client, auth, db: Database
):
    tenant = await _tenant(client, auth)
    actor = await _actor(client, auth)
    body = {"label": "Workbench", "client_id": str(uuid.uuid4()), "tag_token": canonical_token()}
    headers = {**auth("alice"), "Idempotency-Key": "exp"}
    path = f"/v1/tenants/{tenant}/blocks"

    await client.post(path, headers=headers, json=body)
    await _expire_results(db, tenant, actor)
    replay = await client.post(path, headers=headers, json=body)

    assert replay.status_code == 200
    assert "tag_token" not in replay.json()
    assert replay.json()["client_id"] == body["client_id"]
    assert replay.headers["Idempotent-Replay-Source"] == "resource"


async def test_a_200_character_label_is_accepted_on_the_client_form(client, auth):
    """Android's local model already accepts 200; 0.4's 120 stays 120."""
    tenant = await _tenant(client, auth)
    long_label = "x" * 200

    ok = await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"), json={
        "label": long_label, "client_id": str(uuid.uuid4()), "tag_token": canonical_token()})
    legacy = await client.post(f"/v1/tenants/{tenant}/blocks", headers=auth("alice"),
                               json={"label": long_label})

    assert ok.status_code == 201, "the local-first form takes the length Android sends"
    assert legacy.status_code == 400, "the server-minted form keeps its 0.4 limit"
