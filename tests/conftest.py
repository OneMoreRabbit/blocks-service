"""Test fixtures.

Every test runs against a real PostgreSQL, connected as the real unprivileged
runtime role. That is not incidental: forced RLS, the fail-closed behaviour of an
unset context, and role privilege are all properties of the database, and none of
them are observable against a fake or against a superuser connection.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from blocks_service.app import create_app  # noqa: E402
from blocks_service.config import Environment, IdentityMode, Settings  # noqa: E402
from blocks_service.db import Database  # noqa: E402
from blocks_service.identity import DevVerifier  # noqa: E402

TEST_DB = os.environ.get("BLOCKS_TEST_DB", "blocks_service_test")

#: The harness runs as blocks_test_admin, NOT as a superuser.
#:
#: This suite exists to prove tenant isolation, so it must not run as a role that
#: is exempt from it -- a superuser harness can make a broken policy look fine.
#: blocks_test_admin is CREATEDB, NOSUPERUSER, NOBYPASSRLS and a member of
#: blocks_owner: enough to build and tear down test databases, and still subject
#: to every policy under test.
#:
#: Its password is delivered seat-locally by the orchestrator and is deliberately
#: not in this repository, this vault, or the environment defaults.
TEST_ADMIN_SECRET = pathlib.Path.home() / ".secrets" / "blocks_test_admin_password"


def _test_admin_dsn() -> str:
    if dsn := os.environ.get("BLOCKS_TEST_ADMIN_DSN"):
        return dsn
    if not TEST_ADMIN_SECRET.exists():
        raise RuntimeError(
            f"no blocks_test_admin password at {TEST_ADMIN_SECRET} and no "
            "BLOCKS_TEST_ADMIN_DSN set. The superuser this harness used to rely on "
            "was rotated out of the vault; see the database-exposure need."
        )
    password = TEST_ADMIN_SECRET.read_text().strip()
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    return f"host={host} port={port} user=blocks_test_admin password={password} dbname=blocks"


ADMIN_DSN = _test_admin_dsn()
MIGRATOR_PASSWORD = os.environ.get("BLOCKS_DB_MIGRATOR_PASSWORD", "blocks-dev-migrator")
APP_PASSWORD = os.environ.get("BLOCKS_DB_APP_PASSWORD", "blocks-dev-app")
DEV_SECRET = "test-signing-key-not-a-secret-32b+"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """A disposable database, rebuilt from migrations once per session."""
    # A previous run killed mid-test leaves blocks_app connections open, and
    # `drop database ... with (force)` then fails with "permission denied to
    # terminate process": blocks_test_admin is not a member of blocks_app and
    # cannot signal its backends. A role can always terminate its own, so ask
    # blocks_app to clear its own leftovers first. Without this a single
    # interrupted run wedges every subsequent one behind a confusing error.
    host = os.environ.get("BLOCKS_DB_HOST", "postgres")
    port = os.environ.get("BLOCKS_DB_PORT", "5432")
    app_dsn = f"host={host} port={port} user=blocks_app password={APP_PASSWORD} dbname=blocks"
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity"
            " where datname = %s and usename = current_user and pid <> pg_backend_pid()",
            (TEST_DB,),
        )

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{TEST_DB}" with (force)')
        conn.execute(f'create database "{TEST_DB}"')
        # Database-level grants are not cluster globals and do not come with the
        # roles: a freshly created database grants CONNECT/CREATE to nobody. Role
        # bootstrap used to do this, but that needs CREATEROLE, which no seat has.
        conn.execute(
            f'grant connect on database "{TEST_DB}"'
            " to blocks_owner, blocks_migrator, blocks_app"
        )
        conn.execute(f'grant create on database "{TEST_DB}" to blocks_owner')

    admin_test_dsn = _swap_dbname(ADMIN_DSN, TEST_DB)
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "tools" / "dev_db.py"),
            "--admin-dsn", admin_test_dsn,
            # Roles are cluster-level and provisioned by the orchestrator from
            # bootstrap_roles.sql. The harness has no CREATEROLE and needs none.
            "--migrations-only",
        ],
        env={
            **os.environ,
            "BLOCKS_DB_NAME": TEST_DB,
            "BLOCKS_DB_MIGRATOR_PASSWORD": MIGRATOR_PASSWORD,
            "BLOCKS_DB_APP_PASSWORD": APP_PASSWORD,
        },
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"test database setup failed:\n{result.stdout}\n{result.stderr}")

    return Settings(
        env=Environment.test,
        db_name=TEST_DB,
        db_app_password=APP_PASSWORD,
        db_migrator_password=MIGRATOR_PASSWORD,
        identity_mode=IdentityMode.dev,
        dev_token_secret=DEV_SECRET,
    )


def _swap_dbname(dsn: str, dbname: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    return " ".join([*parts, f"dbname={dbname}"])


@pytest.fixture(autouse=True)
def clean_tables(settings: Settings) -> Iterator[None]:
    """Truncate between tests, as the admin role.

    Deliberately not done through the application's own connection: blocks_app
    has no TRUNCATE privilege, and giving it one so the tests were tidier would
    weaken the very boundary under test.
    """
    yield
    with psycopg.connect(_swap_dbname(ADMIN_DSN, TEST_DB), autocommit=True) as conn:
        # TRUNCATE is an owner privilege. blocks_test_admin holds it only through
        # membership of blocks_owner, so it has to ask for it explicitly.
        conn.execute("set role blocks_owner")
        conn.execute(
            "truncate blocks.outbox, blocks.projects, blocks.memberships,"
            " blocks.tenants, blocks.users cascade"
        )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://blocks.test") as c:
            yield c


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.app_dsn)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def token() -> "callable":
    def mint(subject: str, email: str | None = None) -> str:
        return DevVerifier.mint(DEV_SECRET, subject=subject, email=email)

    return mint


@pytest.fixture
def auth(token) -> "callable":
    def headers(subject: str, email: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token(subject, email)}"}

    return headers


@pytest.fixture
def contested(monkeypatch):
    """Hold every caller at the exact contested point: the missing-key read.

    `asyncio.gather` alone proves nothing here: the event loop is free to run one
    request to completion before starting the other, and a sequential schedule
    makes the second request read a stored result through the ordinary path.

    Nor is it enough to hold callers at the entry to `Idempotency.run` — they
    could still serialise across the key lookup, so the test would not have shown
    that both of them saw no result. The defect lives in the window that opens
    when the lookup returns nothing and closes when the reservation is inserted,
    so the barrier belongs inside that window and nowhere else.

    The interception is therefore on `_read`, and only on a read that found
    nothing. `arrived` counts callers that have seen no result, and none of them
    is released until all of them have: when a caller returns from `_read`, every
    participant is provably past the missing-key decision and none has attempted
    its insert. Patched test-side, so no production seam exists for it.

    The losing side of a unique violation re-reads and finds the winner's row.
    That is not a missing-key read, so it never touches the barrier.
    """
    import asyncio

    from blocks_service.idempotency import Idempotency

    original = Idempotency._read

    def arm(participants: int):
        gate = asyncio.Event()
        arrived = []

        async def barriered(self):
            stored = await original(self)
            if stored is None:
                arrived.append(1)
                if len(arrived) >= participants:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=10)
            return stored

        monkeypatch.setattr(Idempotency, "_read", barriered)
        return arrived

    return arm
