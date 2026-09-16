"""Discrete project items (API 0.6), repaired against the architecture review.

Every section below corresponds to a blocking finding in
`meta/blocks-service-api-0-6-review-v0_1.md`, and each test is written against the
failure that finding describes rather than against the happy path, because the
first draft passed 347 tests while still being unsafe for an offline client.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import psycopg
import pytest

from blocks_service.db import Database


async def _tenant(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["personal_tenant_id"]


async def _actor(client, auth, who="alice") -> str:
    return (await client.post("/v1/session", headers=auth(who))).json()["user_id"]


async def _project(client, auth, tenant, who="alice", name="Kitchen") -> str:
    return (await client.post(f"/v1/tenants/{tenant}/projects", headers=auth(who),
                              json={"name": name})).json()["id"]


def _base(tenant, project):
    return f"/v1/tenants/{tenant}/projects/{project}/items"


async def _create_note(client, auth, tenant, project, who="alice", **kw):
    body = {"id": str(uuid.uuid4()), "body": "Wire the third box",
            "operation_id": str(uuid.uuid4())} | kw
    return await client.post(f"{_base(tenant, project)}/notes", headers=auth(who), json=body), body


async def _create_link(client, auth, tenant, project, who="alice", **kw):
    body = {"id": str(uuid.uuid4()), "title": "Regs", "uri": "https://example.com/regs",
            "operation_id": str(uuid.uuid4())} | kw
    return await client.post(f"{_base(tenant, project)}/links", headers=auth(who), json=body), body


def _v(auth, who, version):
    return {**auth(who), "If-Match": str(version)}


async def _events(db, tenant, actor, prefix="project.note"):
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select event_type, payload::text as p from blocks.outbox"
            " where event_type like %s order by id", (prefix + "%",))
        return await cur.fetchall()


# ==========================================================================
# 1. operation_id is a durable identity, not an echo
# ==========================================================================


async def test_an_exact_edit_retry_replays_without_a_second_write_or_event(
    client, auth, db: Database
):
    """The ambiguous-response case: the write succeeded, the client never heard."""
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    created, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"
    envelope = {"body": "four boxes", "operation_id": str(uuid.uuid4())}

    first = await client.patch(path, headers=_v(auth, "alice", 1), json=envelope)
    retry = await client.patch(path, headers=_v(auth, "alice", 1), json=envelope)

    assert first.status_code == 200 and first.json()["row_version"] == 2
    assert retry.status_code == 200, "an exact retry is not a stale-version conflict"
    assert retry.json()["row_version"] == 2, "no second write"
    assert retry.headers.get("Idempotent-Replay") == "true"
    edits = [e for e in await _events(db, tenant, actor) if e["event_type"] == "project.note_edited"]
    assert len(edits) == 1, "no second event"


async def test_reusing_an_operation_id_with_a_different_envelope_conflicts(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"
    op = str(uuid.uuid4())

    await client.patch(path, headers=_v(auth, "alice", 1), json={"body": "a", "operation_id": op})
    other_payload = await client.patch(path, headers=_v(auth, "alice", 2),
                                       json={"body": "b", "operation_id": op})
    assert other_payload.status_code == 409, "one operation id cannot mean two mutations"


async def test_one_operation_id_cannot_be_spent_on_two_resources(client, auth):
    """A client retiring by operation id would retire the wrong queue entry."""
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    op = str(uuid.uuid4())
    first, _ = await _create_note(client, auth, tenant, project, operation_id=op)
    second, _ = await _create_note(client, auth, tenant, project, operation_id=op)

    assert first.status_code == 201
    assert second.status_code == 409


async def test_a_create_with_a_new_operation_id_is_not_a_replay_of_the_old_one(
    client, auth, db: Database
):
    """The draft echoed a new operation id for a create that never happened."""
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)

    again = await client.post(f"{_base(tenant, project)}/notes", headers=auth("alice"),
                              json={**sent, "operation_id": str(uuid.uuid4())})
    assert again.status_code == 409, "same item and content under a new operation is a conflict"
    created = [e for e in await _events(db, tenant, actor) if e["event_type"] == "project.note_created"]
    assert len(created) == 1


async def test_an_exact_create_replay_returns_the_original_and_emits_nothing(
    client, auth, db: Database
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    first, sent = await _create_note(client, auth, tenant, project)
    replay = await client.post(f"{_base(tenant, project)}/notes", headers=auth("alice"), json=sent)

    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["operation_id"] == sent["operation_id"]
    assert len(await _events(db, tenant, actor, "project.note_created")) == 1


# ==========================================================================
# 2. Items are addressed through their project
# ==========================================================================


async def test_an_item_cannot_be_mutated_through_another_projects_url(client, auth):
    """The draft never passed the path project into the update."""
    tenant = await _tenant(client, auth)
    a, b = await _project(client, auth, tenant, name="A"), await _project(client, auth, tenant, name="B")
    _, sent = await _create_note(client, auth, tenant, b, body="belongs to B")

    via_a = await client.patch(f"{_base(tenant, a)}/notes/{sent['id']}",
                               headers=_v(auth, "alice", 1),
                               json={"body": "hijacked", "operation_id": str(uuid.uuid4())})
    assert via_a.status_code == 404

    real = await client.get(f"{_base(tenant, b)}/notes/{sent['id']}", headers=auth("alice"))
    assert real.json()["body"] == "belongs to B" and real.json()["row_version"] == 1


async def test_the_same_item_body_under_another_project_is_not_a_replay(client, auth):
    tenant = await _tenant(client, auth)
    a, b = await _project(client, auth, tenant, name="A"), await _project(client, auth, tenant, name="B")
    _, sent = await _create_note(client, auth, tenant, a)
    under_b = await client.post(f"{_base(tenant, b)}/notes", headers=auth("alice"), json=sent)
    assert under_b.status_code == 409


@pytest.mark.parametrize("case", ["absent", "foreign-tenant", "wrong-project"])
async def test_absent_foreign_and_wrong_project_items_are_one_answer(client, auth, case):
    alice = await _tenant(client, auth, "alice")
    pa = await _project(client, auth, alice)
    if case == "absent":
        project, item = pa, str(uuid.uuid4())
    elif case == "foreign-tenant":
        bob = await _tenant(client, auth, "bob")
        pb = await _project(client, auth, bob, "bob")
        project, item = pa, (await _create_note(client, auth, bob, pb, "bob"))[1]["id"]
    else:
        other = await _project(client, auth, alice, name="Other")
        project, item = pa, (await _create_note(client, auth, alice, other))[1]["id"]

    read = await client.get(f"{_base(alice, project)}/notes/{item}", headers=auth("alice"))
    absent = await client.get(f"{_base(alice, pa)}/notes/{uuid.uuid4()}", headers=auth("alice"))
    assert read.status_code == 404
    assert read.json() == absent.json(), "indistinguishable"


async def test_listing_a_project_that_is_not_there_is_404_not_empty(client, auth):
    tenant = await _tenant(client, auth)
    r = await client.get(f"{_base(tenant, uuid.uuid4())}/notes", headers=auth("alice"))
    assert r.status_code == 404, "an empty list would read as 'no items'"


# ==========================================================================
# 3. The read and acknowledgement shape
# ==========================================================================


async def test_every_result_identifies_tenant_project_item_and_operation(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    created, sent = await _create_note(client, auth, tenant, project)
    body = created.json()
    assert body["tenant_id"] == tenant
    assert body["project_id"] == project
    assert body["id"] == sent["id"]
    assert body["operation_id"] == sent["operation_id"]


@pytest.mark.parametrize("kind", ["notes", "links"])
async def test_individual_items_can_be_read_for_conflict_recovery(client, auth, kind):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    create = _create_note if kind == "notes" else _create_link
    _, sent = await create(client, auth, tenant, project)

    r = await client.get(f"{_base(tenant, project)}/{kind}/{sent['id']}", headers=auth("alice"))
    assert r.status_code == 200
    assert r.json()["id"] == sent["id"] and r.json()["tenant_id"] == tenant


async def test_a_conflict_is_recoverable_through_the_authoritative_read(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"
    await client.patch(path, headers=_v(auth, "alice", 1),
                       json={"body": "theirs", "operation_id": str(uuid.uuid4())})

    stale = await client.patch(path, headers=_v(auth, "alice", 1),
                               json={"body": "mine", "operation_id": str(uuid.uuid4())})
    assert stale.status_code == 409

    current = await client.get(path, headers=auth("alice"))
    assert current.json()["body"] == "theirs" and current.json()["row_version"] == 2


# ==========================================================================
# 4. Navigation aliases
# ==========================================================================


async def test_the_navigation_key_is_returned_on_project_reads(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
                      headers=auth("alice"), json={"project_key": key})

    listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}", headers=auth("alice"))
    assert listed.json()[0]["navigation_key"] == key
    assert page.json()["project"]["navigation_key"] == key


async def test_a_project_carries_one_key_and_a_key_names_one_project(client, auth):
    tenant = await _tenant(client, auth)
    a, b = await _project(client, auth, tenant, name="A"), await _project(client, auth, tenant, name="B")
    key = str(uuid.uuid4())
    base = f"/v1/tenants/{tenant}/projects"
    assert (await client.post(f"{base}/{a}/navigation-aliases", headers=auth("alice"),
                              json={"project_key": key})).status_code == 201

    second_key = await client.post(f"{base}/{a}/navigation-aliases", headers=auth("alice"),
                                   json={"project_key": str(uuid.uuid4())})
    same_key_elsewhere = await client.post(f"{base}/{b}/navigation-aliases", headers=auth("alice"),
                                           json={"project_key": key})
    assert second_key.status_code == 409, "a printed key is never rotated"
    assert same_key_elsewhere.status_code == 409, "a printed key is never repointed"


async def test_a_concurrent_identical_attach_succeeds_rather_than_conflicting(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    key = str(uuid.uuid4())
    path = f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases"

    results = await asyncio.gather(
        client.post(path, headers=auth("alice"), json={"project_key": key}),
        client.post(path, headers=auth("alice"), json={"project_key": key}),
    )
    assert sorted(r.status_code for r in results) in ([200, 201], [201, 201], [200, 200]), (
        "the loser re-reads and returns idempotent success, not a generic conflict"
    )


# ==========================================================================
# 5. Tombstones are terminal
# ==========================================================================


@pytest.mark.parametrize("later", ["edit", "pin", "delete"])
async def test_a_deleted_note_accepts_no_different_mutation(client, auth, db: Database, later):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"
    gone = await client.post(f"{path}/delete", headers=_v(auth, "alice", 1),
                             json={"operation_id": str(uuid.uuid4())})
    assert gone.status_code == 200
    events_before = len(await _events(db, tenant, actor))

    op = {"operation_id": str(uuid.uuid4())}
    if later == "edit":
        r = await client.patch(path, headers=_v(auth, "alice", 2), json={"body": "x", **op})
    elif later == "pin":
        r = await client.put(f"{path}/pin", headers=_v(auth, "alice", 2), json={"pinned": True, **op})
    else:
        r = await client.post(f"{path}/delete", headers=_v(auth, "alice", 2), json=op)

    assert r.status_code == 409
    after = await client.get(path, headers=auth("alice"))
    assert after.json()["row_version"] == 2, "the row did not change"
    assert len(await _events(db, tenant, actor)) == events_before, "no event"


async def test_an_exact_replay_of_the_delete_still_succeeds(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}/delete"
    envelope = {"operation_id": str(uuid.uuid4())}

    first = await client.post(path, headers=_v(auth, "alice", 1), json=envelope)
    replay = await client.post(path, headers=_v(auth, "alice", 1), json=envelope)
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json()["deleted_at"]


# ==========================================================================
# 6. One URI rule, shared by handler and database
# ==========================================================================

ACCEPTED = [
    # allowed schemes, in any case, preserved as written
    "https://example.com/a", "http://example.com/a", "HTTPS://Example.com/a",
    "Http://example.com:8080/x", "hTtPs://example.com",
    # hosts: DNS, IPv4, bracketed IPv6 (with an embedded IPv4 form), ports in range
    "https://localhost", "https://example.com.", "https://a-b.example.co.uk/p?q=1#f",
    "https://192.168.1.10/x", "https://[2001:db8::1]/x", "https://[::ffff:1.2.3.4]:443/?q#f",
    "https://example.com:1/", "https://example.com:65535/",
    # user-info shaped text outside the authority is only path or query
    "https://medium.com/@user", "https://example.com/?who=a@b",
    # obsidian destinations
    "obsidian://open?vault=v&file=f", "OBSIDIAN://vault/note", "obsidian:open",
    "obsidian:///path", "obsidian://?vault=v", "obsidian://open?vault=a@b",
    # surrounding whitespace is trimmed, then judged
    "  https://example.com/trimmed  ",
]
REFUSED = [
    # disallowed schemes
    "file:///etc/passwd", "content://media/x", "intent://scan#Intent;end",
    "javascript:alert(1)", "ftp://example.com/x", "not-a-uri", "https-x://example.com",
    # missing or malformed authority
    "https://", "https:///path", "https:example.com", "https://:443/", "https://example.com\\@evil.com",
    # invalid hosts
    "https://exa mple.com", "https://-bad-.com/x", "https://bad-.com/x", "https://a..b/",
    "https://999.1.1.1/", "https://1.2.3/", "https://01.2.3.4/", "https://[2001:db8::1/x",
    "https://2001:db8::1/x", "https://[2001:db8:::1]/", "https://[fe80::1%25eth0]/",
    "https://" + "a" * 64 + ".com/", "https://exa_mple.com/",
    # invalid ports
    "https://example.com:notaport/x", "https://example.com:99999/x", "https://example.com:0/",
    "https://example.com:/", "https://example.com:65536/", "https://example.com:-1/",
    # user-info
    "https://user:pw@example.com/x", "https://user@example.com/", "http://@example.com/",
    "obsidian://user@vault/note",
    # obsidian without a destination
    "obsidian:", "obsidian://", "obsidian:///", "obsidian://?", "obsidian://#x", "obsidian:?#",
    # control characters, interior whitespace, raw non-ASCII
    "", "   ", "https://example.com/\x00", "https://example.com/a\tb", "https://example.com/a b",
    "https://example.com/\x7f", "https://example.com/\x85x", "https://example.com/\u2003x",
    "https://ex\u00e4mple.com/", "https://example.com/caf\u00e9",
]


@pytest.mark.parametrize("uri", ACCEPTED)
async def test_accepted_uris_are_stored_exactly_as_the_trimmed_value(client, auth, uri):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r, _ = await _create_link(client, auth, tenant, project, uri=uri)
    assert r.status_code == 201, uri
    assert r.json()["uri"] == uri.strip(), "scheme case preserved, only whitespace trimmed"


@pytest.mark.parametrize("uri", REFUSED)
async def test_refused_uris_are_rejected_before_any_write(client, auth, uri):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    r, _ = await _create_link(client, auth, tenant, project, uri=uri)
    assert r.status_code == 400, repr(uri)


def _handler_accepts(uri: str) -> bool:
    from blocks_service.project_items import InvalidItem, validate_uri

    try:
        validate_uri(uri)
        return True
    except InvalidItem:
        return False


async def _database_accepts(conn, tenant, project, link_id, uri) -> tuple[bool, bool]:
    """Try `uri` as a direct insert and as a direct update, as `blocks_app`."""
    decisions = []
    for statement, params in [
        ("insert into blocks.project_resource_links"
         " (tenant_id, id, project_id, title, uri, creation_fingerprint, last_operation_id)"
         " values (%s, %s, %s, 'T', %s, '\\x00', %s)",
         (tenant, uuid.uuid4(), project, uri, uuid.uuid4())),
        ("update blocks.project_resource_links set uri = %s where id = %s", (uri, link_id)),
    ]:
        try:
            async with conn.transaction():
                cur = await conn.execute(statement, params)
                assert cur.rowcount == 1
                raise _Rollback
        except _Rollback:
            decisions.append(True)
        except psycopg.errors.CheckViolation:
            decisions.append(False)
    return tuple(decisions)


class _Rollback(Exception):
    pass


async def test_handler_and_database_decide_every_uri_the_same_way(client, auth, db: Database):
    """The review's parity requirement, through the real role and constraint.

    Each value is judged by the handler and then written straight past it -- an
    insert and an update as `blocks_app` -- so a handler-refused value that the
    database would store fails here, not only one the database would wrongly refuse.
    """
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    created, sent = await _create_link(client, auth, tenant, project)
    assert created.status_code == 201

    async with db.tenant_transaction(tenant, actor) as conn:
        for uri in ACCEPTED:
            assert _handler_accepts(uri), repr(uri)
            assert await _database_accepts(conn, tenant, project, sent["id"], uri.strip()) \
                == (True, True), f"database refused handler-accepted {uri!r}"
        for uri in REFUSED:
            if "\x00" in uri:
                continue   # PostgreSQL text cannot hold NUL at all: refused by the type itself
            assert not _handler_accepts(uri), repr(uri)
            for candidate in {uri, uri.strip()}:
                assert await _database_accepts(conn, tenant, project, sent["id"], candidate) \
                    == (False, False), f"blocks_app could store handler-refused {candidate!r}"


async def test_the_database_constraint_is_the_handler_pattern_byte_for_byte(
    client, auth, db: Database
):
    from blocks_service.project_items import LINK_URI_PATTERN

    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select conname, pg_get_constraintdef(oid) as def from pg_constraint"
            " where conrelid = 'blocks.project_resource_links'::regclass"
            " and conname like '%%uri%%'")
        rows = await cur.fetchall()
    assert [r["conname"] for r in rows] == ["project_resource_links_uri_valid"]
    assert rows[0]["def"] == f"CHECK ((uri ~ '{LINK_URI_PATTERN}'::text))"


def _generated_hosts() -> list[str]:
    import ipaddress
    import random

    rng = random.Random(613)
    hosts = []
    for _ in range(400):
        address = ipaddress.IPv6Address(rng.getrandbits(128) & rng.choice(
            [2**128 - 1, (2**64 - 1) << 64, 0xFFFF_FFFF, 0xFFFF_FFFF_FFFF, 0]))
        text = rng.choice([address.compressed, address.exploded])
        for _ in range(rng.randint(0, 2)):
            i = rng.randrange(len(text) + 1)
            text = rng.choice([text[:i] + rng.choice(":.0fg") + text[i:], text[:i] + text[i + 1:]])
        hosts.append(text)
        v4 = ".".join(str(rng.choice([rng.randint(0, 255), rng.randint(256, 999)])) for _ in range(4))
        hosts.append(v4)
        hosts.append(address.exploded.rsplit(":", 2)[0] + ":" + v4)
    return hosts


def test_the_ip_literal_rules_agree_with_the_standard_library():
    import ipaddress

    from blocks_service.project_items import _IPV4, _IPV6

    for host in _generated_hosts():
        for pattern, parse in [(_IPV6, ipaddress.IPv6Address), (_IPV4, ipaddress.IPv4Address)]:
            try:
                parse(host)
                expected = True
            except ValueError:
                expected = False
            assert bool(re.fullmatch(pattern, host)) is expected, (pattern is _IPV6, host)


async def test_postgres_and_python_evaluate_the_pattern_identically_on_generated_hosts(
    client, auth, db: Database
):
    from blocks_service.project_items import LINK_URI_PATTERN

    uris = [f"https://{h}/" for h in _generated_hosts()] + \
           [f"https://[{h}]:8443/" for h in _generated_hosts()]
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            "select u, u ~ %s as ok from unnest(%s::text[]) as u", (LINK_URI_PATTERN, uris))
        database = {r["u"]: r["ok"] for r in await cur.fetchall()}
    assert {u: bool(re.fullmatch(LINK_URI_PATTERN, u)) for u in uris} == database


# ==========================================================================
# Authored text, events, and the clean break
# ==========================================================================


async def test_no_authored_text_reaches_an_event_or_an_error(client, auth, db: Database):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    secret_body, secret_title = "boiler is behind the panel", "private supplier pricing"
    secret_uri = "https://example.com/secret-tender"
    await _create_note(client, auth, tenant, project, body=secret_body)
    await _create_link(client, auth, tenant, project, title=secret_title, uri=secret_uri)

    feed = str(await _events(db, tenant, actor, "project."))
    for secret in (secret_body, secret_title, secret_uri):
        assert secret not in feed

    refused, _ = await _create_link(client, auth, tenant, project,
                                    uri="ftp://internal.example.com/confidential")
    assert refused.status_code == 400 and "confidential" not in refused.text


async def test_the_0_4_singleton_and_0_6_items_never_touch_each_other(client, auth):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}", headers=auth("alice"))
    base = page.json()["notes"]["row_version"]

    saved = await client.put(f"/v1/tenants/{tenant}/projects/{project}/notes",
                             headers={**auth("alice"), "If-Match": str(base)},
                             json={"body": "singleton content"})
    assert saved.status_code == 200
    items = await client.get(f"{_base(tenant, project)}/notes", headers=auth("alice"))
    assert items.json() == [], "a 0.4 save creates no 0.6 item"

    await _create_note(client, auth, tenant, project, body="discrete item")
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}", headers=auth("alice"))
    assert page.json()["notes"]["body"] == "singleton content", "the singleton is untouched"


async def test_the_creation_identity_is_untouched_by_attaching_a_key(client, auth):
    tenant = await _tenant(client, auth)
    cid = str(uuid.uuid4())
    project = (await client.post(f"/v1/tenants/{tenant}/projects", headers=auth("alice"),
                                 json={"name": "K", "client_id": cid})).json()["id"]
    await client.post(f"/v1/tenants/{tenant}/projects/{project}/navigation-aliases",
                      headers=auth("alice"), json={"project_key": str(uuid.uuid4())})
    page = await client.get(f"/v1/tenants/{tenant}/projects/{project}", headers=auth("alice"))
    assert page.json()["project"]["client_id"] == cid


async def test_a_foreign_key_resolves_exactly_as_an_unknown_one(client, auth):
    alice, bob = await _tenant(client, auth, "alice"), await _tenant(client, auth, "bob")
    pb = await _project(client, auth, bob, "bob")
    key = str(uuid.uuid4())
    await client.post(f"/v1/tenants/{bob}/projects/{pb}/navigation-aliases",
                      headers=auth("bob"), json={"project_key": key})
    foreign = await client.post(f"/v1/tenants/{alice}/projects/resolve-key",
                                headers=auth("alice"), json={"project_key": key})
    unknown = await client.post(f"/v1/tenants/{alice}/projects/resolve-key",
                                headers=auth("alice"), json={"project_key": str(uuid.uuid4())})
    assert foreign.status_code == 404 and foreign.json() == unknown.json()


async def test_deletion_is_a_tombstone_in_the_schema_not_just_the_handler(
    client, auth, db: Database
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    await _create_note(client, auth, tenant, project)
    async with db.tenant_transaction(tenant, actor) as conn:
        with pytest.raises(psycopg.errors.Error):
            async with conn.transaction():
                await conn.execute("delete from blocks.project_note_items")


# ==========================================================================
# 7. A replay acknowledges the requested operation, never the row's latest one
# ==========================================================================
#
# Operation A is accepted, operation B then changes the same item, and an offline
# client retries A after an ambiguous response. The response must identify A --
# the outbox entry the client may retire -- while carrying the current state that
# B produced, with no write and no event.


async def _state(db, tenant, actor, table, item_id):
    async with db.tenant_transaction(tenant, actor) as conn:
        cur = await conn.execute(
            f"select row_version, updated_at, last_operation_id from blocks.{table}"
            " where id = %s", (item_id,))
        row = await cur.fetchone()
        receipts = await (await conn.execute(
            "select count(*) as n from blocks.project_item_operations"
            " where item_id = %s", (item_id,))).fetchone()
        events = await (await conn.execute(
            "select count(*) as n from blocks.outbox where payload->>'itemId' = %s",
            (str(item_id),))).fetchone()
        return row, receipts["n"], events["n"]


def _assert_acknowledges_a_over_b(replay, a, b, version):
    assert replay.status_code in (200, 201)
    assert replay.headers.get("Idempotent-Replay") == "true"
    body = replay.json()
    assert body["operation_id"] == a
    assert body["last_operation_id"] == b
    assert body["row_version"] == version


NOTE_B_MUTATIONS = {
    "edit": ("patch", "", {"body": "second"}),
    "pin": ("put", "/pin", {"pinned": True}),
    "delete": ("post", "/delete", {}),
}


@pytest.mark.parametrize("mutation_b", sorted(NOTE_B_MUTATIONS))
async def test_retrying_note_edit_a_after_b_acknowledges_a(
    client, auth, db: Database, mutation_b
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    created, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"

    a = {"body": "first", "operation_id": str(uuid.uuid4())}
    assert (await client.patch(path, headers=_v(auth, "alice", 1), json=a)).status_code == 200
    method, suffix, payload = NOTE_B_MUTATIONS[mutation_b]
    b = payload | {"operation_id": str(uuid.uuid4())}
    accepted_b = await getattr(client, method)(path + suffix, headers=_v(auth, "alice", 2), json=b)
    assert accepted_b.status_code == 200
    assert accepted_b.json()["operation_id"] == accepted_b.json()["last_operation_id"] == b["operation_id"]
    before = await _state(db, tenant, actor, "project_note_items", sent["id"])

    replay = await client.patch(path, headers=_v(auth, "alice", 1), json=a)

    _assert_acknowledges_a_over_b(replay, a["operation_id"], b["operation_id"], 3)
    assert replay.json() | {"operation_id": b["operation_id"]} == accepted_b.json()
    assert await _state(db, tenant, actor, "project_note_items", sent["id"]) == before


async def test_retrying_note_pin_a_after_b_acknowledges_a(client, auth, db: Database):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_note(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/notes/{sent['id']}"

    a = {"pinned": True, "operation_id": str(uuid.uuid4())}
    assert (await client.put(path + "/pin", headers=_v(auth, "alice", 1), json=a)).status_code == 200
    b = {"body": "later", "operation_id": str(uuid.uuid4())}
    assert (await client.patch(path, headers=_v(auth, "alice", 2), json=b)).status_code == 200
    before = await _state(db, tenant, actor, "project_note_items", sent["id"])

    replay = await client.put(path + "/pin", headers=_v(auth, "alice", 1), json=a)

    _assert_acknowledges_a_over_b(replay, a["operation_id"], b["operation_id"], 3)
    assert replay.json()["body"] == "later"
    assert await _state(db, tenant, actor, "project_note_items", sent["id"]) == before


@pytest.mark.parametrize("kind", ["note", "link"])
async def test_retrying_a_create_after_a_later_edit_acknowledges_the_create(
    client, auth, db: Database, kind
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    make = _create_note if kind == "note" else _create_link
    created, sent = await make(client, auth, tenant, project)
    assert created.status_code == 201
    path = f"{_base(tenant, project)}/{kind}s/{sent['id']}"
    b = ({"body": "edited"} if kind == "note"
         else {"title": "Edited", "uri": "https://example.com/b"}) | {"operation_id": str(uuid.uuid4())}
    assert (await client.patch(path, headers=_v(auth, "alice", 1), json=b)).status_code == 200
    table = "project_note_items" if kind == "note" else "project_resource_links"
    before = await _state(db, tenant, actor, table, sent["id"])

    replay = await client.post(f"{_base(tenant, project)}/{kind}s", headers=auth("alice"), json=sent)

    assert replay.status_code == 201
    _assert_acknowledges_a_over_b(replay, sent["operation_id"], b["operation_id"], 2)
    assert await _state(db, tenant, actor, table, sent["id"]) == before


@pytest.mark.parametrize("mutation_b", ["edit", "delete"])
async def test_retrying_link_edit_a_after_b_acknowledges_a(
    client, auth, db: Database, mutation_b
):
    tenant, actor = await _tenant(client, auth), await _actor(client, auth)
    project = await _project(client, auth, tenant)
    _, sent = await _create_link(client, auth, tenant, project)
    path = f"{_base(tenant, project)}/links/{sent['id']}"

    a = {"title": "A", "uri": "https://example.com/a", "operation_id": str(uuid.uuid4())}
    assert (await client.patch(path, headers=_v(auth, "alice", 1), json=a)).status_code == 200
    if mutation_b == "edit":
        b = {"title": "B", "uri": "https://example.com/b", "operation_id": str(uuid.uuid4())}
        accepted_b = await client.patch(path, headers=_v(auth, "alice", 2), json=b)
    else:
        b = {"operation_id": str(uuid.uuid4())}
        accepted_b = await client.post(path + "/delete", headers=_v(auth, "alice", 2), json=b)
    assert accepted_b.status_code == 200
    before = await _state(db, tenant, actor, "project_resource_links", sent["id"])

    replay = await client.patch(path, headers=_v(auth, "alice", 1), json=a)

    _assert_acknowledges_a_over_b(replay, a["operation_id"], b["operation_id"], 3)
    assert replay.json() | {"operation_id": b["operation_id"]} == accepted_b.json()
    assert await _state(db, tenant, actor, "project_resource_links", sent["id"]) == before


@pytest.mark.parametrize("kind", ["note", "link"])
async def test_reads_carry_state_provenance_not_an_acknowledgement(client, auth, kind):
    tenant = await _tenant(client, auth)
    project = await _project(client, auth, tenant)
    make = _create_note if kind == "note" else _create_link
    _, sent = await make(client, auth, tenant, project)
    base = f"{_base(tenant, project)}/{kind}s"

    for body in [(await client.get(f"{base}/{sent['id']}", headers=auth("alice"))).json(),
                 (await client.get(base, headers=auth("alice"))).json()[0]]:
        assert body["last_operation_id"] == sent["operation_id"]
        assert "operation_id" not in body


def test_the_contract_separates_the_acknowledged_operation_from_the_state():
    from blocks_service.app import create_app

    schemas = create_app().openapi()["components"]["schemas"]
    for read, ack in [("NoteOut", "NoteAck"), ("LinkOut", "LinkAck")]:
        assert "operation_id" not in schemas[read]["properties"]
        assert "last_operation_id" in schemas[read]["required"]
        assert {"operation_id", "last_operation_id"} <= set(schemas[ack]["required"])
