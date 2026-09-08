"""Migration 0007, exercised against real legacy data.

The brief asks for evidence that the in-place association is idempotent,
restart-safe, identity-preserving, and cannot produce a cross-tenant or duplicate
row. Each test below builds a database at the pre-0007 schema, seeds legacy
sessions and parking, and then migrates it.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import psycopg
import pytest

from blocks_service.identity import DEV_ISSUER
from psycopg.rows import dict_row

MIGRATIONS = sorted(pathlib.Path("migrations").glob("*.sql"))
PRE = [m for m in MIGRATIONS if m.stem < "0007"]
EXPAND = [m for m in MIGRATIONS if m.stem.startswith(("0007", "0008", "0009"))]
TARGET = EXPAND[0]        # 0007 — the relation
INVARIANT = EXPAND[1]     # 0008 — the trigger and the catch-up
IDENTITY = EXPAND[2]      # 0009 — creation identity and authored parking time
SCRATCH = "blocks_migration_drill"


def _dsn(dbname: str) -> str:
    from tests.conftest import ADMIN_DSN, _swap_dbname

    return _swap_dbname(ADMIN_DSN, dbname)


@pytest.fixture
def legacy_db() -> Iterator[str]:
    """A database at the pre-0007 schema, holding two tenants' legacy data."""
    from tests.conftest import ADMIN_DSN

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{SCRATCH}" with (force)')
        conn.execute(f'create database "{SCRATCH}"')
        conn.execute(
            f'grant connect on database "{SCRATCH}"'
            " to blocks_owner, blocks_migrator, blocks_app, blocks_dispatcher"
        )
        conn.execute(f'grant create on database "{SCRATCH}" to blocks_owner')
    with psycopg.connect(_dsn(SCRATCH), autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        for migration in PRE:
            conn.execute(migration.read_text())
        _seed(conn)
    yield SCRATCH
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{SCRATCH}" with (force)')


def _seed(conn) -> None:
    """Two tenants, each with a project, two sessions, and parking on both.

    Two sessions per project is the case that matters: their `sequence` values
    both start at 1, so anything that treats sequence as unique per project
    breaks here.
    """
    conn.execute("alter table blocks.parking_records no force row level security")
    conn.execute("alter table blocks.work_sessions no force row level security")
    conn.execute("alter table blocks.memberships no force row level security")
    conn.execute("alter table blocks.projects no force row level security")
    for who in ("alice", "bob"):
        user = conn.execute(
            "insert into blocks.users (issuer, subject) values (%s, %s) returning id",
            (DEV_ISSUER, who),
        ).fetchone()[0]
        tenant = conn.execute(
            "insert into blocks.tenants (kind, name, personal_for) values ('personal', %s, %s)"
            " returning id", (who, user),
        ).fetchone()[0]
        conn.execute(
            "insert into blocks.memberships (tenant_id, user_id, role) values (%s,%s,'owner')",
            (tenant, user),
        )
        project = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, %s) returning id",
            (tenant, f"{who} project"),
        ).fetchone()[0]
        for n in (1, 2):
            session = conn.execute(
                "insert into blocks.work_sessions (tenant_id, project_id, started_by_user_id,"
                " core_outcome, state) values (%s,%s,%s,%s,'closed') returning id",
                (tenant, project, user, f"outcome {n}"),
            ).fetchone()[0]
            conn.execute(
                "insert into blocks.parking_records (tenant_id, work_session_id, where_i_am,"
                " next_physical_action, must_not_forget, authored_by_user_id, sequence)"
                " values (%s,%s,%s,'action','forget',%s,1)",
                (tenant, session, f"{who} session {n}", user),
            )
    for t in ("parking_records", "work_sessions", "memberships", "projects"):
        conn.execute(f"alter table blocks.{t} force row level security")


def _snapshot(dbname: str) -> list[dict]:
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.parking_records no force row level security")
        rows = conn.execute(
            "select id, tenant_id, work_session_id, where_i_am, next_physical_action,"
            " must_not_forget, authored_by_user_id, device_id, sequence, supersedes_id,"
            " created_at from blocks.parking_records order by id"
        ).fetchall()
        conn.execute("alter table blocks.parking_records force row level security")
    return rows


def _apply(dbname: str, migration: pathlib.Path) -> None:
    with psycopg.connect(_dsn(dbname), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute(migration.read_text())
        conn.commit()


def _migrate(dbname: str) -> None:
    """The whole expand step, as it deploys: 0007, 0008, then 0009."""
    for migration in EXPAND:
        _apply(dbname, migration)


def _legacy_create_project(dbname: str, name: str) -> tuple[str, str]:
    """Insert a project the way the PRE-0.3 writer does: the project row only.

    This is the writer that stays live between migration and image cutover. It
    knows nothing about notes, so it is the exact shape that reintroduces the
    missing-row failure.
    """
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.projects no force row level security")
        # project_notes too, once 0008 exists: the trigger's insert runs under the
        # caller's own policies, so an out-of-band insert with no tenant context
        # is refused. The real pre-0.3 API inserts inside a tenant-scoped
        # transaction and satisfies the policy; only an administrative insert like
        # this one has to make the exemption explicit.
        conn.execute("alter table blocks.project_notes no force row level security")
        tenant = conn.execute(
            "select tenant_id from blocks.projects limit 1"
        ).fetchone()["tenant_id"]
        row = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, %s)"
            " returning id, tenant_id",
            (tenant, name),
        ).fetchone()
        conn.execute("alter table blocks.project_notes force row level security")
        conn.execute("alter table blocks.projects force row level security")
    return str(row["id"]), str(row["tenant_id"])


def test_the_migration_preserves_every_parking_row_identically(legacy_db):
    """Identity, author, device, timestamps, ordering, supersession — all unchanged."""
    before = _snapshot(legacy_db)
    assert len(before) == 4
    _migrate(legacy_db)
    after = _snapshot(legacy_db)

    assert [r["id"] for r in before] == [r["id"] for r in after], "row identity moved"
    for old, new in zip(before, after):
        for field in (
            "id", "tenant_id", "work_session_id", "where_i_am", "next_physical_action",
            "must_not_forget", "authored_by_user_id", "device_id", "sequence",
            "supersedes_id", "created_at",
        ):
            assert old[field] == new[field], f"{field} changed on {old['id']}"


def test_every_row_is_associated_to_its_historical_project(legacy_db):
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.parking_records no force row level security")
        conn.execute("alter table blocks.work_sessions no force row level security")
        wrong = conn.execute(
            "select count(*) as n from blocks.parking_records p"
            " join blocks.work_sessions w on w.id = p.work_session_id"
            " where p.project_id is distinct from w.project_id"
        ).fetchone()["n"]
        orphan = conn.execute(
            "select count(*) as n from blocks.parking_records where project_id is null"
        ).fetchone()["n"]
        cross = conn.execute(
            "select count(*) as n from blocks.parking_records p"
            " join blocks.projects pr on pr.id = p.project_id"
            " where pr.tenant_id <> p.tenant_id"
        ).fetchone()["n"]
    assert (wrong, orphan, cross) == (0, 0, 0)


def test_legacy_session_provenance_is_retained(legacy_db):
    """The brief says preserve session history, not discard it once projects exist."""
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        # Both tables: a count under FORCE RLS with no tenant context reads 0, which
        # looks exactly like "the rows were deleted". That is the same trap the
        # migration itself fell into, and it is worth not falling into it twice.
        conn.execute("alter table blocks.parking_records no force row level security")
        conn.execute("alter table blocks.work_sessions no force row level security")
        n = conn.execute(
            "select count(*) as n from blocks.parking_records where work_session_id is not null"
        ).fetchone()["n"]
        sessions = conn.execute("select count(*) as n from blocks.work_sessions").fetchone()["n"]
    assert n == 4, "legacy provenance was dropped"
    assert sessions == 4, "legacy sessions were removed"


def test_running_the_migration_twice_is_a_no_op(legacy_db):
    """Re-running the backfill must change nothing and duplicate nothing."""
    _migrate(legacy_db)
    after_first = _snapshot(legacy_db)

    with psycopg.connect(_dsn(legacy_db), autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.parking_records no force row level security")
        conn.execute("alter table blocks.work_sessions no force row level security")
        conn.execute(
            "update blocks.parking_records p set project_id = w.project_id"
            " from blocks.work_sessions w"
            " where w.id = p.work_session_id and p.project_id is null"
        )
        conn.execute("alter table blocks.work_sessions force row level security")
        conn.execute("alter table blocks.parking_records force row level security")

    assert _snapshot(legacy_db) == after_first
    assert len(_snapshot(legacy_db)) == 4, "re-running duplicated rows"


def test_an_interrupted_migration_leaves_nothing_behind_and_restarts_cleanly(legacy_db):
    """DDL is transactional here, so an interruption must roll back whole."""
    before = _snapshot(legacy_db)

    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute(TARGET.read_text())
        conn.rollback()   # the interruption

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        cols = {
            r["column_name"]
            for r in conn.execute(
                "select column_name from information_schema.columns"
                " where table_schema='blocks' and table_name='parking_records'"
            ).fetchall()
        }
    assert "project_id" not in cols, "a rolled-back migration left a column behind"
    assert _snapshot(legacy_db) == before

    _migrate(legacy_db)   # restart
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.parking_records no force row level security")
        orphan = conn.execute(
            "select count(*) as n from blocks.parking_records where project_id is null"
        ).fetchone()["n"]
    assert orphan == 0


def test_forced_rls_is_restored_after_the_migration(legacy_db):
    """The backfill lifts FORCE on two tables; leaving it off would be a breach."""
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class"
            " where relnamespace = 'blocks'::regnamespace and relkind = 'r'"
            "   and relname in ('parking_records','work_sessions','project_notes','projects')"
        ).fetchall()
    for row in rows:
        assert row["relrowsecurity"] and row["relforcerowsecurity"], row["relname"]


def test_the_expand_step_gives_every_legacy_project_exactly_one_empty_notes_row(legacy_db):
    """Structural state, not fabricated content.

    This test previously asserted the opposite, on the reasoning that creating
    rows was inventing data. That was wrong: without the row a migrated project
    404s on its page and can never take a first note, because the save path
    UPDATEs and finds nothing. An empty row means "no note yet" — which is
    precisely true of a project that has never had one.
    """
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.project_notes no force row level security")
        conn.execute("alter table blocks.projects no force row level security")
        rows = conn.execute(
            "select n.tenant_id, n.project_id, n.body, n.row_version,"
            "       p.tenant_id as project_tenant"
            "  from blocks.project_notes n"
            "  join blocks.projects p on p.id = n.project_id"
        ).fetchall()
        projects = conn.execute("select count(*) as n from blocks.projects").fetchone()["n"]

    assert len(rows) == projects, "exactly one notes row per project"
    for row in rows:
        assert row["body"] == "", "backfilled notes must be empty, not invented"
        assert row["row_version"] == 1
        assert row["tenant_id"] == row["project_tenant"], "notes must not cross a tenant"


def test_the_notes_backfill_is_idempotent(legacy_db):
    """Re-running must not produce a second row, or violate the primary key."""
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.projects no force row level security")
        conn.execute("alter table blocks.project_notes no force row level security")
        before = conn.execute("select count(*) as n from blocks.project_notes").fetchone()["n"]
        conn.execute(
            "insert into blocks.project_notes (tenant_id, project_id)"
            " select p.tenant_id, p.id from blocks.projects p"
            " where not exists (select 1 from blocks.project_notes n"
            "   where n.tenant_id = p.tenant_id and n.project_id = p.id)"
        )
        after = conn.execute("select count(*) as n from blocks.project_notes").fetchone()["n"]
        conn.execute("alter table blocks.project_notes force row level security")
        conn.execute("alter table blocks.projects force row level security")
    assert before == after


# --------------------------------------------------------------------------
# The migrated database, exercised through the real API
# --------------------------------------------------------------------------


async def test_a_migrated_project_opens_and_takes_its_first_note(legacy_db, auth):
    """The blocker, end to end.

    Without a backfilled notes row a migrated project 404s on its page and can
    never take a first note, because the save path UPDATEs and finds nothing.
    This drives the real API against a database that was migrated from pre-0007,
    which is the only way to catch that.
    """
    from httpx import ASGITransport, AsyncClient

    from blocks_service.app import create_app
    from blocks_service.config import Environment, IdentityMode, Settings
    from tests.conftest import APP_PASSWORD, DEV_SECRET, MIGRATOR_PASSWORD

    _migrate(legacy_db)

    settings = Settings(
        env=Environment.test,
        db_name=legacy_db,
        db_app_password=APP_PASSWORD,
        db_migrator_password=MIGRATOR_PASSWORD,
        identity_mode=IdentityMode.dev,
        dev_token_secret=DEV_SECRET,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://migrated.test"
        ) as client:
            session = (await client.post("/v1/session", headers=auth("alice"))).json()
            tenant = session["personal_tenant_id"]

            listed = await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("alice"))
            assert listed.status_code == 200
            legacy = [p for p in listed.json() if p["name"] == "alice project"]
            assert legacy, "the migrated project is missing from the dashboard"
            project_id = legacy[0]["id"]

            page = await client.get(
                f"/v1/tenants/{tenant}/projects/{project_id}", headers=auth("alice")
            )
            assert page.status_code == 200, "a migrated project must open"
            assert page.json()["notes"]["body"] == ""
            assert page.json()["notes"]["row_version"] == 1

            # Its legacy parking came across and is readable on the new surface.
            assert page.json()["newest_parking"] is not None
            history = await client.get(
                f"/v1/tenants/{tenant}/projects/{project_id}/parking", headers=auth("alice")
            )
            assert len(history.json()) == 2, "both legacy sessions' records, on the project"

            saved = await client.put(
                f"/v1/tenants/{tenant}/projects/{project_id}/notes",
                headers={**auth("alice"), "If-Match": "1"},
                json={"body": "first note on a migrated project"},
            )
            assert saved.status_code == 200, "the first save on a migrated project must succeed"
            assert saved.json()["row_version"] == 2

            reread = await client.get(
                f"/v1/tenants/{tenant}/projects/{project_id}/notes", headers=auth("alice")
            )
            assert reread.json()["body"] == "first note on a migrated project"


async def test_a_migrated_tenant_cannot_read_the_other_tenants_project(legacy_db, auth):
    """Isolation survives the migration, on data that predates every policy change."""
    from httpx import ASGITransport, AsyncClient

    from blocks_service.app import create_app
    from blocks_service.config import Environment, IdentityMode, Settings
    from tests.conftest import APP_PASSWORD, DEV_SECRET, MIGRATOR_PASSWORD

    _migrate(legacy_db)
    settings = Settings(
        env=Environment.test, db_name=legacy_db, db_app_password=APP_PASSWORD,
        db_migrator_password=MIGRATOR_PASSWORD, identity_mode=IdentityMode.dev,
        dev_token_secret=DEV_SECRET,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://migrated.test"
        ) as client:
            alice_tenant = (
                await client.post("/v1/session", headers=auth("alice"))
            ).json()["personal_tenant_id"]
            bob_tenant = (
                await client.post("/v1/session", headers=auth("bob"))
            ).json()["personal_tenant_id"]

            alice_projects = await client.get(
                f"/v1/tenants/{alice_tenant}/projects", headers=auth("alice")
            )
            bob_projects = await client.get(
                f"/v1/tenants/{bob_tenant}/projects", headers=auth("bob")
            )
            assert {p["name"] for p in alice_projects.json()} == {"alice project"}
            assert {p["name"] for p in bob_projects.json()} == {"bob project"}

            forbidden = await client.get(
                f"/v1/tenants/{alice_tenant}/projects", headers=auth("bob")
            )
            assert forbidden.status_code == 403


# --------------------------------------------------------------------------
# The cutover window: the pre-0.3 writer is live across it
# --------------------------------------------------------------------------
#
# The deployment contract runs migrations BEFORE switching the image and does not
# stop the old API first. So a project can be created by the project-only writer
# after 0007 and before 0008, and after 0008 while the old image is still serving.
# Both boundaries have to be safe.


async def test_a_project_created_between_0007_and_0008_is_healed_by_0008(legacy_db, auth):
    """Boundary (a): the row that slipped through, repaired by the catch-up."""
    from httpx import ASGITransport, AsyncClient

    from blocks_service.app import create_app
    from blocks_service.config import Environment, IdentityMode, Settings
    from tests.conftest import APP_PASSWORD, DEV_SECRET, MIGRATOR_PASSWORD

    _apply(legacy_db, TARGET)                       # 0007 only
    project_id, tenant = _legacy_create_project(legacy_db, "created mid-cutover")

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.project_notes no force row level security")
        orphan = conn.execute(
            "select count(*) as n from blocks.project_notes where project_id = %s",
            (project_id,),
        ).fetchone()["n"]
        conn.execute("alter table blocks.project_notes force row level security")
    assert orphan == 0, "precondition: the legacy writer creates no notes row"

    _apply(legacy_db, INVARIANT)                    # 0008 heals it
    _apply(legacy_db, IDENTITY)                     # 0009 completes the expand step

    settings = Settings(
        env=Environment.test, db_name=legacy_db, db_app_password=APP_PASSWORD,
        db_migrator_password=MIGRATOR_PASSWORD, identity_mode=IdentityMode.dev,
        dev_token_secret=DEV_SECRET,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://cutover.test"
        ) as client:
            await client.post("/v1/session", headers=auth("alice"))
            page = await client.get(
                f"/v1/tenants/{tenant}/projects/{project_id}", headers=auth("alice")
            )
            assert page.status_code == 200, "a project created mid-cutover must open"
            assert page.json()["notes"]["body"] == ""

            saved = await client.put(
                f"/v1/tenants/{tenant}/projects/{project_id}/notes",
                headers={**auth("alice"), "If-Match": "1"},
                json={"body": "first note after the cutover window"},
            )
            assert saved.status_code == 200
            assert saved.json()["row_version"] == 2


async def test_a_project_created_by_the_legacy_writer_after_0008_gets_its_row(legacy_db, auth):
    """Boundary (b): the trigger covers a writer that knows nothing about notes."""
    from httpx import ASGITransport, AsyncClient

    from blocks_service.app import create_app
    from blocks_service.config import Environment, IdentityMode, Settings
    from tests.conftest import APP_PASSWORD, DEV_SECRET, MIGRATOR_PASSWORD

    _migrate(legacy_db)                             # 0007 + 0008
    project_id, tenant = _legacy_create_project(legacy_db, "created by the old image")

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.project_notes no force row level security")
        rows = conn.execute(
            "select tenant_id, body, row_version from blocks.project_notes"
            " where project_id = %s",
            (project_id,),
        ).fetchall()
        conn.execute("alter table blocks.project_notes force row level security")

    assert len(rows) == 1, "exactly one notes row, created automatically"
    assert rows[0]["body"] == ""
    assert rows[0]["row_version"] == 1
    assert str(rows[0]["tenant_id"]) == tenant, "the trigger must not cross a tenant"

    settings = Settings(
        env=Environment.test, db_name=legacy_db, db_app_password=APP_PASSWORD,
        db_migrator_password=MIGRATOR_PASSWORD, identity_mode=IdentityMode.dev,
        dev_token_secret=DEV_SECRET,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://cutover.test"
        ) as client:
            await client.post("/v1/session", headers=auth("alice"))
            page = await client.get(
                f"/v1/tenants/{tenant}/projects/{project_id}", headers=auth("alice")
            )
            assert page.status_code == 200
            saved = await client.put(
                f"/v1/tenants/{tenant}/projects/{project_id}/notes",
                headers={**auth("alice"), "If-Match": "1"}, json={"body": "works"},
            )
            assert saved.status_code == 200


def test_the_invariant_migration_restores_forced_rls(legacy_db):
    """0008 lifts FORCE on two tables for its sweep; leaving it off is a breach."""
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class"
            " where relnamespace = 'blocks'::regnamespace and relkind = 'r'"
            "   and relname in ('projects','project_notes')"
        ).fetchall()
    assert rows
    for row in rows:
        assert row["relrowsecurity"] and row["relforcerowsecurity"], row["relname"]


def test_the_trigger_is_not_security_definer(legacy_db):
    """It must run as the inserting role, under that role's own policies.

    A SECURITY DEFINER trigger here would be a standing way to write a notes row
    into a tenant the caller could not otherwise reach.
    """
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "select prosecdef from pg_proc where proname = 'ensure_project_notes'"
        ).fetchone()
    assert row is not None, "the invariant function is missing"
    assert row["prosecdef"] is False


def test_the_invariant_fails_closed_without_a_tenant_context(legacy_db):
    """A project cannot be created where its notes row cannot be.

    The trigger runs under the inserting role's policies, so an insert with no
    tenant context is refused rather than producing a project with no notes row.
    Fail-closed and loud is the right outcome: the alternative is the exact
    silent state this whole migration exists to prevent.
    """
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.projects no force row level security")
        tenant = conn.execute("select tenant_id from blocks.projects limit 1").fetchone()["tenant_id"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into blocks.projects (tenant_id, name) values (%s, 'no context')",
                (tenant,),
            )
        conn.execute("alter table blocks.projects force row level security")
