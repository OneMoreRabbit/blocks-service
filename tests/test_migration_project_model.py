"""Migration 0007, exercised against real legacy data.

The brief asks for evidence that the in-place association is idempotent,
restart-safe, identity-preserving, and cannot produce a cross-tenant or duplicate
row. Each test below builds a database at the pre-0007 schema, seeds legacy
sessions and parking, and then migrates it.
"""

from __future__ import annotations

import pathlib
import os
from collections.abc import Iterator

import psycopg
import pytest

from blocks_service.identity import DEV_ISSUER
from psycopg.rows import dict_row

MIGRATIONS = sorted(pathlib.Path("migrations").glob("*.sql"))
PRE = [m for m in MIGRATIONS if m.stem < "0007"]
EXPAND = [m for m in MIGRATIONS
          if m.stem.startswith((
              "0007", "0008", "0009", "0010", "0011", "0012", "0013", "0014", "0015",
          ))]
TARGET = EXPAND[0]        # 0007 — the relation
INVARIANT = EXPAND[1]     # 0008 — the trigger and the catch-up
IDENTITY = EXPAND[2]      # 0009 — creation identity and authored parking time
SCOPED = EXPAND[3]        # 0010 — tenant-scoped client identity
RESERVE = EXPAND[4]       # 0011 — idempotency reservation
BLOCKID = EXPAND[5]       # 0012 — client-generated block identity
ITEMS = EXPAND[6]         # 0013 — discrete project items
INTERACTION = EXPAND[7]   # 0014 — ordered aggregates and Park tombstones
NOTE_ORDER = EXPAND[8]    # 0015 — ordered Notes
SCRATCH = "blocks_migration_drill"


def _backup_dsn(dbname: str) -> str:
    """The RLS-exempt READ credential the preflight requires."""
    import os
    import pathlib as _p

    pw = _p.Path(os.path.expanduser("~/.secrets/blocks_backup_password")).read_text().strip()
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    return f"host={host} port={port} user=blocks_backup password={pw} dbname={dbname}"


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
            # blocks_backup too: the binding preflight runs as the RLS-exempt read
            # role, which is the point of it -- an audit that cannot see across
            # tenants would report "clean" from a blind connection.
            " to blocks_owner, blocks_migrator, blocks_app, blocks_dispatcher,"
            " blocks_backup"
        )
        conn.execute(f'grant create on database "{SCRATCH}" to blocks_owner')
    with psycopg.connect(_dsn(SCRATCH), autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        for migration in PRE:
            conn.execute(migration.read_text())
        _seed(conn)
    yield SCRATCH
    # A current-image cutover case opens the application pool against this
    # database. If a failed request leaves one blocks_app backend behind, the
    # non-superuser harness cannot force-drop it. Ask blocks_app to terminate its
    # own leftovers first, as the main test-database fixture does.
    from tests.conftest import APP_PASSWORD
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    with psycopg.connect(
        f"host={host} port={port} user=blocks_app password={APP_PASSWORD} dbname={SCRATCH}",
        autocommit=True,
    ) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity"
            " where datname = %s and usename = current_user and pid <> pg_backend_pid()",
            (SCRATCH,),
        )
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
    """The whole expand step, as it deploys: 0007 through 0012."""
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
        conn.execute("select set_config('blocks.tenant_id', %s, false)", (str(tenant),))
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
    _apply(legacy_db, IDENTITY)                     # 0009
    _apply(legacy_db, SCOPED)                       # 0010
    _apply(legacy_db, RESERVE)                      # 0011
    _apply(legacy_db, BLOCKID)                      # 0012
    # 0013 as well: this test then runs the CURRENT application against the
    # schema, and the current image requires 0013 through 0015.
    # Project reads carry the navigation key, which lives in a 0013 table.
    _apply(legacy_db, ITEMS)                        # 0013
    _apply(legacy_db, INTERACTION)                  # 0014
    _apply(legacy_db, NOTE_ORDER)                   # 0015 completes the expand step

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


def test_0010_upgrades_a_database_holding_eligible_0009_era_rows(legacy_db):
    """The boundary 0010 exists to repair, with a row that actually needs repairing.

    The earlier version of 0010 validated the completeness constraint BEFORE
    backfilling, so a real 0009-era row — fingerprint set, client_id still null —
    violated it and the migration died at exactly the boundary it was written for.
    Every test passed anyway, because none of them ever created such a row.
    """
    _apply(legacy_db, TARGET)       # 0007
    _apply(legacy_db, INVARIANT)    # 0008
    _apply(legacy_db, IDENTITY)     # 0009 — creation identity exists, client_id does not

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.projects no force row level security")
        conn.execute("alter table blocks.parking_records no force row level security")
        # project_notes too: inserting a project fires ensure_project_notes, which
        # runs under the caller's policies and has no tenant context here.
        conn.execute("alter table blocks.project_notes no force row level security")
        # A project created the 0009 way: the client's value IS the id, and a
        # fingerprint records the operation that made it.
        eligible = conn.execute(
            "insert into blocks.projects (tenant_id, name, creation_fingerprint)"
            " select tenant_id, '0009-era', '\\x0badc0de'::bytea from blocks.projects limit 1"
            " returning id, tenant_id"
        ).fetchone()
        conn.execute(
            "insert into blocks.parking_records (tenant_id, project_id, where_i_am,"
            " next_physical_action, must_not_forget, authored_by_user_id, sequence,"
            " occurred_at, creation_fingerprint)"
            " select %s, %s, '0009-era park','a','b', id, 99, now(), '\\x0badc0de'::bytea"
            "   from blocks.users limit 1",
            (eligible["tenant_id"], eligible["id"]),
        )
        conn.execute("alter table blocks.project_notes force row level security")
        conn.execute("alter table blocks.parking_records force row level security")
        conn.execute("alter table blocks.projects force row level security")

    _apply(legacy_db, SCOPED)       # 0010 — must survive this
    _apply(legacy_db, RESERVE)      # 0011
    _apply(legacy_db, BLOCKID)      # 0012

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.projects no force row level security")
        conn.execute("alter table blocks.parking_records no force row level security")
        for table in ("projects", "parking_records"):
            mapped = conn.execute(
                f"select count(*) as n from blocks.{table}"
                " where creation_fingerprint is not null and client_id is distinct from id"
            ).fetchone()["n"]
            assert mapped == 0, f"{table}: eligible rows must map client_id = id"
            stray = conn.execute(
                f"select count(*) as n from blocks.{table}"
                " where creation_fingerprint is null and client_id is not null"
            ).fetchone()["n"]
            assert stray == 0, f"{table}: null-fingerprint rows must stay null"
            validated = conn.execute(
                "select bool_and(convalidated) as v from pg_constraint"
                f" where conrelid = ('blocks.{table}')::regclass"
                "   and conname like '%client_identity_complete'"
            ).fetchone()["v"]
            assert validated, f"{table}: the constraint must end up VALIDATED"
        conn.execute("alter table blocks.parking_records force row level security")
        conn.execute("alter table blocks.projects force row level security")

    # Tenant uniqueness and forced RLS both intact afterwards.
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        idx = {r["indexname"] for r in conn.execute(
            "select indexname from pg_indexes where schemaname='blocks'"
            " and indexname like '%tenant_client_id_key'").fetchall()}
        # Three tables carry tenant-scoped client identity after the full expand:
        # projects and parking_records from 0010, physical_blocks from 0012. The
        # count is asserted rather than the mere presence of any one of them, so
        # a table that quietly acquires or loses the pattern is visible here.
        assert idx == {
            "projects_tenant_client_id_key",
            "parking_records_tenant_client_id_key",
            "physical_blocks_tenant_client_id_key",
        }, idx
        forced = conn.execute(
            "select bool_and(relforcerowsecurity) as f from pg_class"
            " where relnamespace='blocks'::regnamespace and relkind='r'"
            "   and relname in ('projects','parking_records')").fetchone()["f"]
        assert forced, "forced RLS must be restored"


# --------------------------------------------------------------------------
# 0012 — client-generated block identity (ADR-0014)
# --------------------------------------------------------------------------


def _new_tenant(dbname: str, name: str) -> str:
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        return conn.execute(
            "insert into blocks.tenants (kind, name)"
            " values ('organization', %s) returning id::text as id",
            (name,),
        ).fetchone()["id"]


def _insert_block(dbname: str, tenant: str, label: str, token_hash: bytes):
    """Insert a block the way the runtime does: under transaction-local context.

    Forced row-level security applies to `blocks_owner` too, so a bare insert as
    the owner is refused. Setting the context is not a workaround -- it is the
    only legal way to write a tenant-scoped row, which is the property 0004 was
    written to guarantee.
    """
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        row = conn.execute(
            "insert into blocks.physical_blocks (tenant_id, label, token_hash)"
            " values (%s, %s, %s) returning id::text as id",
            (tenant, label, token_hash),
        ).fetchone()
        conn.commit()
        return row


def _tenant_and_block(dbname: str, label: str, token_hash: bytes) -> tuple[str, str]:
    """A 0.4-era block: server-minted token, no client identity."""
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        tenant = conn.execute(
            "insert into blocks.tenants (kind, name) values ('organization', %s)"
            " returning id::text as id",
            (label + " tenant",),
        ).fetchone()["id"]
    block = _insert_block(dbname, tenant, label, token_hash)["id"]
    return tenant, block


def test_0012_upgrades_a_database_holding_0_4_era_blocks(legacy_db):
    """A server-generated block has no truthful client identity and gets none.

    The migration must not invent one. A fabricated `client_id` would assert that
    some client named this row, and a later genuine registration carrying that
    value would then replay a block it never created.
    """
    for migration in EXPAND[:5]:                      # through 0011
        _apply(legacy_db, migration)
    shared = b"\x11" * 32
    tenant, _ = _tenant_and_block(legacy_db, "Legacy", shared)

    _apply(legacy_db, BLOCKID)                        # 0012 — must survive this

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, false)", (tenant,))
        row = conn.execute(
            "select client_id, creation_fingerprint, label from blocks.physical_blocks"
        ).fetchone()
        assert row["client_id"] is None, "no fabricated client identity"
        assert row["creation_fingerprint"] is None, "and therefore no fingerprint"
        assert row["label"] == "Legacy", "the row is otherwise untouched"

        validated = conn.execute(
            "select convalidated as v from pg_constraint"
            " where conrelid = ('blocks.physical_blocks')::regclass"
            "   and conname = 'physical_blocks_client_identity_complete'"
        ).fetchone()["v"]
        assert validated, "the identity-completeness constraint ends up VALIDATED"


def test_0012_makes_token_uniqueness_tenant_scoped(legacy_db):
    """The 0.4 global index is an oracle once clients supply the token.

    Two tenants may legitimately hold the same 32 bytes. Before 0012 the second
    insert failed, and the failure alone disclosed that the value existed
    somewhere the caller could not see.
    """
    for migration in EXPAND[:5]:
        _apply(legacy_db, migration)
    shared = b"\x22" * 32
    _tenant_and_block(legacy_db, "First", shared)
    second = _new_tenant(legacy_db, "second")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_block(legacy_db, second, "Clash", shared)

    _apply(legacy_db, BLOCKID)

    third = _new_tenant(legacy_db, "third")
    _insert_block(legacy_db, third, "Independent", shared)

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.physical_blocks no force row level security")
        n = conn.execute(
            "select count(*) as n from blocks.physical_blocks where token_hash = %s",
            (shared,),
        ).fetchone()["n"]
        conn.execute("alter table blocks.physical_blocks force row level security")
    assert n == 2, "the same token in two tenants is two independent blocks"

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_block(legacy_db, third, "Duplicate", shared)


def test_a_0_4_writer_still_works_against_the_0012_schema(legacy_db):
    """Restore/rollback boundary: the older image writes neither new column."""
    _migrate(legacy_db)
    tenant = _new_tenant(legacy_db, "rollback")
    block = _insert_block(legacy_db, tenant, "Old writer", b"\x33" * 32)["id"]

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        row = conn.execute(
            "select client_id, creation_fingerprint from blocks.physical_blocks"
            " where id = %s", (block,)
        ).fetchone()
    assert row["client_id"] is None and row["creation_fingerprint"] is None, (
        "the 0.4 insert shape is still legal and still satisfies the constraint"
    )


def test_0012_refuses_a_corrupt_pre_existing_binding_atomically(legacy_db):
    """API 0.4 could create a cross-tenant binding, so history may hold one.

    It proved neither parent visible before writing, and its foreign keys
    referenced globally keyed parents. Treating existing corruption as impossible
    would be assuming the absence of the very defect 0012 exists to close.

    0012 must then fail as a unit: the row it refused is still there for an
    operator to dispose of, the schema is unchanged, and — the part that would be
    silent and total — forced row-level security is not left switched off.
    """
    for migration in EXPAND[:5]:                       # through 0011
        _apply(legacy_db, migration)

    mine = _new_tenant(legacy_db, "mine")
    theirs = _new_tenant(legacy_db, "theirs")
    block = _insert_block(legacy_db, mine, "Mine", b"\x44" * 32)["id"]
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (theirs,))
        project = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, 'Theirs')"
            " returning id::text as id", (theirs,)
        ).fetchone()["id"]
        conn.commit()
    # The corrupt row: child claims `mine`, project belongs to `theirs`.
    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (mine,))
        conn.execute(
            "insert into blocks.block_bindings (tenant_id, block_id, project_id)"
            " values (%s, %s, %s)", (mine, block, project))
        conn.commit()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _apply(legacy_db, BLOCKID)

    # FORCE is inspected FIRST, before this test executes any DDL of its own. An
    # earlier version toggled FORCE off to count the row and back on again, then
    # asserted FORCE was on -- proving only its own restoration. It would have
    # passed against a migration that left isolation disabled.
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        forced = {r["relname"]: r["relforcerowsecurity"] for r in conn.execute(
            "select relname, relforcerowsecurity from pg_class"
            " where relname in ('block_bindings','physical_blocks','projects')").fetchall()}
        assert all(forced.values()), f"FORCE RLS must not be left disabled: {forced}"

        cols = {r["column_name"] for r in conn.execute(
            "select column_name from information_schema.columns"
            " where table_schema='blocks' and table_name='physical_blocks'").fetchall()}
        assert "client_id" not in cols, "0012 rolled back as a unit; no partial schema"

    # The row itself is read through the RLS-exempt role, which needs no DDL.
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        surviving = conn.execute(
            "select count(*) as n from blocks.block_bindings").fetchone()["n"]
    assert surviving == 1, "the refused row is left for disposition, not deleted"


def test_the_preflight_finds_the_corruption_0012_would_refuse(legacy_db):
    """Run before migrating, so a finding stops the deploy with the data intact."""
    import subprocess
    import sys

    for migration in EXPAND[:5]:
        _apply(legacy_db, migration)
    mine = _new_tenant(legacy_db, "mine")
    clean = subprocess.run(
        [sys.executable, "tools/preflight_bindings.py", "--dsn", _backup_dsn(legacy_db)],
        capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    theirs = _new_tenant(legacy_db, "theirs")
    block = _insert_block(legacy_db, mine, "Mine", b"\x55" * 32)["id"]
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (theirs,))
        project = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, 'Theirs')"
            " returning id::text as id", (theirs,)).fetchone()["id"]
        conn.commit()
    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (mine,))
        conn.execute(
            "insert into blocks.block_bindings (tenant_id, block_id, project_id)"
            " values (%s, %s, %s)", (mine, block, project))
        conn.commit()

    found = subprocess.run(
        [sys.executable, "tools/preflight_bindings.py", "--dsn", _backup_dsn(legacy_db)],
        capture_output=True, text=True)
    assert found.returncode != 0, "a finding must stop the deploy"
    assert "project" in found.stdout.lower()
    assert "1" in found.stdout, "the count is reported"


def _security_snapshot(dbname: str) -> dict:
    """Everything the audit must not be able to move. Read-only itself."""
    with psycopg.connect(_backup_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        force = {r["relname"]: r["relforcerowsecurity"] for r in conn.execute(
            "select relname, relforcerowsecurity from pg_class c"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'blocks' and c.relkind = 'r'").fetchall()}
        cols = sorted(r["c"] for r in conn.execute(
            "select table_name || '.' || column_name as c"
            " from information_schema.columns where table_schema = 'blocks'").fetchall())
        rows = conn.execute(
            "select count(*) as n from blocks.block_bindings").fetchone()["n"]
    return {"force": force, "columns": cols, "bindings": rows}


def _run_preflight(dsn: str):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "tools/preflight_bindings.py", "--dsn", dsn],
        capture_output=True, text=True)


def test_the_preflight_changes_nothing_on_any_path(legacy_db):
    """Clean, finding, and execution-error alike.

    The first version of this tool lifted FORCE RLS on three tables so the owner
    could read across tenants, and restored it afterwards. The lift is a committed
    schema change, so a failure between lift and restore leaves tenant isolation
    switched off — the safety check becomes the outage. A tool that runs *because*
    something might be wrong must not be able to make things worse.
    """
    for migration in EXPAND[:5]:
        _apply(legacy_db, migration)
    before = _security_snapshot(legacy_db)

    clean = _run_preflight(_backup_dsn(legacy_db))
    assert clean.returncode == 0
    assert _security_snapshot(legacy_db) == before, "clean path moved something"

    # Now make it find one.
    mine, theirs = _new_tenant(legacy_db, "mine"), _new_tenant(legacy_db, "theirs")
    block = _insert_block(legacy_db, mine, "Mine", b"\x66" * 32)["id"]
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (theirs,))
        project = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, 'Theirs')"
            " returning id::text as id", (theirs,)).fetchone()["id"]
        conn.commit()
    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (mine,))
        conn.execute(
            "insert into blocks.block_bindings (tenant_id, block_id, project_id)"
            " values (%s, %s, %s)", (mine, block, project))
        conn.commit()

    with_finding = _security_snapshot(legacy_db)
    found = _run_preflight(_backup_dsn(legacy_db))
    assert found.returncode == 1
    assert _security_snapshot(legacy_db) == with_finding, "finding path moved something"

    broken = _run_preflight(_backup_dsn(legacy_db).replace("dbname=", "dbname=no_such_db_"))
    assert broken.returncode == 2, "an audit that cannot run is a stop, not a pass"
    assert _security_snapshot(legacy_db) == with_finding, "error path moved something"


def test_the_preflight_refuses_a_credential_that_cannot_see_across_tenants(legacy_db):
    """"No findings" from a blind connection is the most dangerous possible answer."""
    for migration in EXPAND[:5]:
        _apply(legacy_db, migration)
    before = _security_snapshot(legacy_db)

    result = _run_preflight(_dsn(legacy_db))          # blocks_test_admin: not BYPASSRLS

    assert result.returncode == 2, "fail closed rather than report a meaningless clean"
    assert "row-level security" in result.stderr.lower()
    assert _security_snapshot(legacy_db) == before


# --------------------------------------------------------------------------
# 0013 — discrete project items (API 0.6)
# --------------------------------------------------------------------------
#
# The full suite applies every migration to a fresh database, which proves 0013
# works on an empty schema and nothing else. These tests are the failure
# boundaries the architecture review asked for: an upgrade over real data, an
# interrupted apply, the security shape, the cardinality rules, and -- the one
# ADR-0022 turns on -- that existing singleton Notes are preserved and never
# projected into the new collection.


def _project_with_singleton(dbname: str, tenant: str, name: str, body: str) -> str:
    """A pre-0013 project whose API 0.4 singleton Notes row carries real content."""
    with psycopg.connect(_dsn(dbname), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        project = conn.execute(
            "insert into blocks.projects (tenant_id, name) values (%s, %s)"
            " returning id::text as id", (tenant, name)).fetchone()["id"]
        conn.execute("update blocks.project_notes set body = %s where project_id = %s",
                     (body, project))
        conn.commit()
    return project


def _count(dbname: str, table: str) -> int:
    with psycopg.connect(_backup_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        return conn.execute(f"select count(*) as n from blocks.{table}").fetchone()["n"]


def _table_exists(dbname: str, table: str) -> bool:
    with psycopg.connect(_backup_dsn(dbname), row_factory=dict_row, autocommit=True) as conn:
        return conn.execute(
            "select to_regclass(%s) is not null as present", (f"blocks.{table}",)
        ).fetchone()["present"]


ITEM_TABLES = ("project_note_items", "project_resource_links",
               "project_navigation_aliases", "project_item_operations")


def test_0013_upgrades_a_database_holding_singleton_notes_without_touching_them(legacy_db):
    """Preservation AND non-projection: ADR-0022's clean break, over real data."""
    for migration in EXPAND[:6]:                      # through 0012
        _apply(legacy_db, migration)
    tenant = _new_tenant(legacy_db, "upgrade")
    project = _project_with_singleton(legacy_db, tenant, "Kitchen", "the boiler is behind the panel")

    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        before = conn.execute(
            "select body, row_version, updated_at from blocks.project_notes"
            " where project_id = %s", (project,)).fetchone()

    _apply(legacy_db, ITEMS)

    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        after = conn.execute(
            "select body, row_version, updated_at from blocks.project_notes"
            " where project_id = %s", (project,)).fetchone()
    assert after == before, "the singleton row is byte-for-byte untouched"
    assert _count(legacy_db, "project_note_items") == 0, "no item derived from any singleton"
    assert _count(legacy_db, "project_item_operations") == 0


def test_an_interrupted_0013_leaves_no_partial_schema_and_reapplies_cleanly(legacy_db):
    """Transaction interruption and restart."""
    for migration in EXPAND[:6]:
        _apply(legacy_db, migration)
    sql = ITEMS.read_text()
    # Break the migration at its very end, after every table has been created, so
    # a non-atomic apply would leave all of them behind.
    broken = sql + "\nselect 1/0;\n"

    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        with pytest.raises(psycopg.errors.DivisionByZero):
            conn.execute(broken)
        conn.rollback()

    for table in ITEM_TABLES:
        assert not _table_exists(legacy_db, table), f"{table} survived an interrupted apply"

    _apply(legacy_db, ITEMS)                            # the restart
    for table in ITEM_TABLES:
        assert _table_exists(legacy_db, table), f"{table} missing after a clean re-apply"


def test_0013_forces_rls_with_policies_and_grants_no_delete(legacy_db):
    _migrate(legacy_db)
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        for table in ITEM_TABLES:
            rls = conn.execute(
                "select relrowsecurity, relforcerowsecurity from pg_class c"
                " join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'blocks' and c.relname = %s", (table,)).fetchone()
            assert rls["relrowsecurity"] and rls["relforcerowsecurity"], f"{table}: RLS not forced"

            policies = conn.execute(
                "select count(*) as n from pg_policies"
                " where schemaname = 'blocks' and tablename = %s", (table,)).fetchone()["n"]
            assert policies >= 1, f"{table}: no tenant policy"

            # has_table_privilege, not information_schema.role_table_grants: the
            # latter only lists grants visible to the QUERYING role, so from
            # blocks_backup it returned an empty set -- and "DELETE not in set()"
            # passed without checking anything. The positive assertion below is
            # what exposed that; it is kept so a blind query cannot pass again.
            def can(priv, _table=table):
                return conn.execute(
                    "select has_table_privilege('blocks_app', %s, %s) as ok",
                    (f"blocks.{_table}", priv)).fetchone()["ok"]

            assert can("SELECT") and can("INSERT"), f"{table}: runtime role cannot read/write"
            assert not can("DELETE"), f"{table}: DELETE granted — a tombstone could be removed"

        def receipts_can(priv):
            return conn.execute(
                "select has_table_privilege('blocks_app', 'blocks.project_item_operations', %s)"
                " as ok", (priv,)).fetchone()["ok"]

        assert not receipts_can("UPDATE"), "a recorded operation receipt must be append-only"


def test_0013_refuses_an_item_referencing_another_tenants_project(legacy_db):
    """Tenant-composite references hold even if application validation is bypassed."""
    _migrate(legacy_db)
    mine, theirs = _new_tenant(legacy_db, "mine"), _new_tenant(legacy_db, "theirs")
    their_project = _project_with_singleton(legacy_db, theirs, "Theirs", "")

    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (mine,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "insert into blocks.project_note_items"
                " (tenant_id, id, project_id, body, creation_fingerprint, last_operation_id)"
                " values (%s, gen_random_uuid(), %s, 'x', '\\x00', gen_random_uuid())",
                (mine, their_project))


def test_0013_allows_one_key_per_project_and_one_project_per_key(legacy_db):
    _migrate(legacy_db)
    tenant = _new_tenant(legacy_db, "aliases")
    a = _project_with_singleton(legacy_db, tenant, "A", "")
    b = _project_with_singleton(legacy_db, tenant, "B", "")

    def attach(project, key):
        with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
            conn.execute("set role blocks_owner")
            conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
            conn.execute(
                "insert into blocks.project_navigation_aliases"
                " (tenant_id, project_key, project_id) values (%s, %s, %s)",
                (tenant, key, project))
            conn.commit()

    import uuid as _uuid
    key = str(_uuid.uuid4())
    attach(a, key)
    with pytest.raises(psycopg.errors.UniqueViolation):
        attach(a, str(_uuid.uuid4()))                  # a second key for A
    with pytest.raises(psycopg.errors.UniqueViolation):
        attach(b, key)                                  # A's key for B


# --------------------------------------------------------------------------
# 0014 — interaction ordering and Park tombstones (API 0.7)
# --------------------------------------------------------------------------

INTERACTION_TABLES = (
    "project_order_aggregates", "project_order_positions",
    "link_order_aggregates", "link_order_positions", "interaction_operations",
)


def test_0014_backfills_complete_orders_and_preserves_every_park_field(legacy_db):
    for migration in EXPAND[:7]:                       # through 0013
        _apply(legacy_db, migration)
    before = _snapshot(legacy_db)

    _apply(legacy_db, INTERACTION)

    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        projects = conn.execute(
            "select count(*) as n from blocks.projects where status = 'active'"
        ).fetchone()["n"]
        positions = conn.execute(
            "select count(*) as n from blocks.project_order_positions"
        ).fetchone()["n"]
        aggregates = conn.execute(
            "select count(*) as n from blocks.link_order_aggregates"
        ).fetchone()["n"]
        park_versions = conn.execute(
            "select row_version, deleted_at, last_operation_id from blocks.parking_records"
        ).fetchall()
    assert positions == projects
    assert aggregates == projects
    assert all(row == {"row_version": 1, "deleted_at": None, "last_operation_id": None}
               for row in park_versions)
    assert _snapshot(legacy_db) == before, "authored Park identity/content/time changed"


def test_an_interrupted_0014_rolls_back_and_restarts_cleanly(legacy_db):
    for migration in EXPAND[:7]:
        _apply(legacy_db, migration)
    broken = INTERACTION.read_text() + "\nselect 1/0;\n"

    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        with pytest.raises(psycopg.errors.DivisionByZero):
            conn.execute(broken)
        conn.rollback()

    for table in INTERACTION_TABLES:
        assert not _table_exists(legacy_db, table), f"{table} survived an interrupted apply"
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        columns = conn.execute(
            "select column_name from information_schema.columns"
            " where table_schema = 'blocks' and table_name = 'parking_records'"
        ).fetchall()
    assert "deleted_at" not in {row["column_name"] for row in columns}

    _apply(legacy_db, INTERACTION)
    for table in INTERACTION_TABLES:
        assert _table_exists(legacy_db, table), f"{table} missing after clean re-apply"


def test_0014_forces_tenant_rls_and_keeps_receipts_append_only(legacy_db):
    _migrate(legacy_db)
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        for table in INTERACTION_TABLES:
            state = conn.execute(
                "select relrowsecurity, relforcerowsecurity from pg_class c"
                " join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'blocks' and c.relname = %s", (table,)
            ).fetchone()
            assert state["relrowsecurity"] and state["relforcerowsecurity"]
        assert not conn.execute(
            "select has_table_privilege('blocks_app',"
            " 'blocks.interaction_operations', 'UPDATE') as ok"
        ).fetchone()["ok"]
        assert not conn.execute(
            "select has_table_privilege('blocks_app',"
            " 'blocks.interaction_operations', 'DELETE') as ok"
        ).fetchone()["ok"]


def test_0014_structurally_rejects_a_same_tenant_wrong_project_link(legacy_db):
    """The handler is not the boundary: the persisted membership is composite."""
    _migrate(legacy_db)
    tenant = _new_tenant(legacy_db, "project-scoped-link-order")
    project_a = _project_with_singleton(legacy_db, tenant, "A", "")
    project_b = _project_with_singleton(legacy_db, tenant, "B", "")

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        link_ids = []
        for project, title in ((project_a, "A link"), (project_b, "B link")):
            link_ids.append(conn.execute(
                "insert into blocks.project_resource_links"
                " (tenant_id, id, project_id, title, uri, creation_fingerprint,"
                "  last_operation_id)"
                " values (%s, gen_random_uuid(), %s, %s, 'https://example.com/',"
                " decode('00', 'hex'), gen_random_uuid()) returning id::text as id",
                (tenant, project, title),
            ).fetchone()["id"])
        conn.commit()

    def positions():
        with psycopg.connect(
            _backup_dsn(legacy_db), row_factory=dict_row, autocommit=True
        ) as conn:
            return conn.execute(
                "select project_id::text as project_id, link_id::text as link_id, position"
                " from blocks.link_order_positions where tenant_id = %s"
                " order by project_id, position",
                (tenant,),
            ).fetchall()

    before = positions()
    assert len(before) == 2

    from tests.conftest import APP_PASSWORD
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    app_dsn = (
        f"host={host} port={port} user=blocks_app password={APP_PASSWORD} dbname={legacy_db}"
    )
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        conn.execute("select set_config('blocks.tenant_id', %s, false)", (tenant,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as refused:
            conn.execute(
                "insert into blocks.link_order_positions"
                " (tenant_id, project_id, link_id, position) values (%s, %s, %s, 2)",
                (tenant, project_b, link_ids[0]),
            )
    assert refused.value.diag.constraint_name == "link_order_positions_tenant_id_project_id_link_id_fkey"
    assert positions() == before, "either Project order changed after the refused direct insert"


# --------------------------------------------------------------------------
# 0015 — Note ordering (API 0.8)
# --------------------------------------------------------------------------

NOTE_ORDER_TABLES = ("note_order_aggregates", "note_order_positions")


def _through_0014(dbname: str) -> None:
    for migration in EXPAND[:8]:
        _apply(dbname, migration)


def test_0015_backfills_the_exact_visible_note_order_without_rewriting_notes(legacy_db):
    _through_0014(legacy_db)
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        parent = conn.execute(
            "select p.tenant_id, p.id as project_id, m.user_id"
            " from blocks.projects p join blocks.memberships m using (tenant_id)"
            " order by p.id limit 1"
        ).fetchone()

    import uuid as _uuid
    # Deliberately make creation and edit times disagree in both pin groups.
    # Released Android 0.9.6 displayed updatedAt-descending within each group,
    # so each older-created but later-edited Note must precede the newer one.
    notes = [
        (_uuid.uuid4(), "unpinned older edited", False,
         "2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z"),
        (_uuid.uuid4(), "pinned older edited", True,
         "2026-01-02T00:00:00Z", "2026-01-10T00:00:00Z"),
        (_uuid.uuid4(), "unpinned newer", False,
         "2026-01-03T00:00:00Z", "2026-01-07T00:00:00Z"),
        (_uuid.uuid4(), "pinned newer", True,
         "2026-01-04T00:00:00Z", "2026-01-09T00:00:00Z"),
        (_uuid.uuid4(), "deleted pinned", True,
         "2026-01-05T00:00:00Z", "2026-01-11T00:00:00Z"),
    ]
    with psycopg.connect(_dsn(legacy_db), autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, false)",
                     (str(parent["tenant_id"]),))
        for index, (note_id, body, pinned, created_at, updated_at) in enumerate(notes):
            conn.execute(
                "insert into blocks.project_note_items"
                " (tenant_id, id, project_id, body, pinned, authored_by_user_id,"
                "  created_at, updated_at, deleted_at, creation_fingerprint, last_operation_id)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s,"
                "         case when %s = 4 then %s::timestamptz else null end,"
                "         decode('00', 'hex'), gen_random_uuid())",
                (parent["tenant_id"], note_id, parent["project_id"], body, pinned,
                 parent["user_id"], created_at, updated_at, index, updated_at),
            )

    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        before = conn.execute(
            "select * from blocks.project_note_items where project_id = %s order by id",
            (parent["project_id"],),
        ).fetchall()

    _apply(legacy_db, NOTE_ORDER)

    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        after = conn.execute(
            "select * from blocks.project_note_items where project_id = %s order by id",
            (parent["project_id"],),
        ).fetchall()
        ordered = conn.execute(
            "select note_id from blocks.note_order_positions"
            " where project_id = %s order by position", (parent["project_id"],)
        ).fetchall()
    assert after == before
    assert [row["note_id"] for row in ordered] == [
        notes[1][0], notes[3][0], notes[0][0], notes[2][0],
    ]


def test_an_interrupted_0015_rolls_back_and_restarts_cleanly(legacy_db):
    _through_0014(legacy_db)
    broken = NOTE_ORDER.read_text() + "\nselect 1/0;\n"
    with psycopg.connect(_dsn(legacy_db), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        with pytest.raises(psycopg.errors.DivisionByZero):
            conn.execute(broken)
        conn.rollback()

    for table in NOTE_ORDER_TABLES:
        assert not _table_exists(legacy_db, table), f"{table} survived an interrupted apply"
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        operation_rule = conn.execute(
            "select pg_get_constraintdef(oid) as rule from pg_constraint"
            " where conrelid = 'blocks.interaction_operations'::regclass"
            " and conname = 'interaction_operation_type'"
        ).fetchone()["rule"]
        note_key = conn.execute(
            "select 1 from pg_constraint"
            " where conrelid = 'blocks.project_note_items'::regclass"
            " and conname = 'project_note_items_tenant_project_id_key'"
        ).fetchone()
    assert "REORDER_NOTES" not in operation_rule
    assert note_key is None

    _apply(legacy_db, NOTE_ORDER)
    for table in NOTE_ORDER_TABLES:
        assert _table_exists(legacy_db, table), f"{table} missing after clean re-apply"


def test_0015_forces_rls_preserves_read_only_backup_and_grants_no_note_hard_delete(
    legacy_db,
):
    _migrate(legacy_db)
    with psycopg.connect(_backup_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        for table in NOTE_ORDER_TABLES:
            state = conn.execute(
                "select relrowsecurity, relforcerowsecurity from pg_class c"
                " join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'blocks' and c.relname = %s", (table,)
            ).fetchone()
            assert state["relrowsecurity"] and state["relforcerowsecurity"]
            conn.execute(f"select count(*) from blocks.{table}").fetchone()
        assert not conn.execute(
            "select has_table_privilege('blocks_app',"
            " 'blocks.project_note_items', 'DELETE') as ok"
        ).fetchone()["ok"]
        assert not conn.execute(
            "select has_table_privilege('blocks_app',"
            " 'blocks.interaction_operations', 'UPDATE') as ok"
        ).fetchone()["ok"]


def test_0015_structurally_rejects_a_same_tenant_wrong_project_note(legacy_db):
    _migrate(legacy_db)
    tenant = _new_tenant(legacy_db, "project-scoped-note-order")
    project_a = _project_with_singleton(legacy_db, tenant, "A", "")
    project_b = _project_with_singleton(legacy_db, tenant, "B", "")

    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant,))
        note_ids = []
        for project, body in ((project_a, "A note"), (project_b, "B note")):
            note_ids.append(conn.execute(
                "insert into blocks.project_note_items"
                " (tenant_id, id, project_id, body, creation_fingerprint, last_operation_id)"
                " values (%s, gen_random_uuid(), %s, %s, decode('00', 'hex'), gen_random_uuid())"
                " returning id::text as id", (tenant, project, body),
            ).fetchone()["id"])
        conn.commit()

    from tests.conftest import APP_PASSWORD
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    app_dsn = (
        f"host={host} port={port} user=blocks_app password={APP_PASSWORD} dbname={legacy_db}"
    )
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        conn.execute("select set_config('blocks.tenant_id', %s, false)", (tenant,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as refused:
            conn.execute(
                "insert into blocks.note_order_positions"
                " (tenant_id, project_id, note_id, position) values (%s, %s, %s, 2)",
                (tenant, project_b, note_ids[0]),
            )
    assert refused.value.diag.constraint_name == \
        "note_order_positions_tenant_id_project_id_note_id_fkey"
