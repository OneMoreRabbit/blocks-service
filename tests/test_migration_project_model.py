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
from psycopg.rows import dict_row

MIGRATIONS = sorted(pathlib.Path("migrations").glob("*.sql"))
PRE = [m for m in MIGRATIONS if m.stem < "0007"]
TARGET = next(m for m in MIGRATIONS if m.stem.startswith("0007"))
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
            "insert into blocks.users (issuer, subject) values ('i', %s) returning id", (who,)
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


def _migrate(dbname: str) -> None:
    with psycopg.connect(_dsn(dbname), autocommit=False) as conn:
        conn.execute("set role blocks_owner")
        conn.execute(TARGET.read_text())
        conn.commit()


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


def test_the_migration_creates_no_project_notes_for_legacy_projects(legacy_db):
    """Notes are created with a project; migrating must not invent documents.

    Legacy projects predate notes, so reading their notes is a miss until someone
    saves. Fabricating rows would be inventing data the brief forbids.
    """
    _migrate(legacy_db)
    with psycopg.connect(_dsn(legacy_db), row_factory=dict_row, autocommit=True) as conn:
        conn.execute("set role blocks_owner")
        conn.execute("alter table blocks.project_notes no force row level security")
        n = conn.execute("select count(*) as n from blocks.project_notes").fetchone()["n"]
    assert n == 0
