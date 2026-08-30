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

TEST_DB = os.environ.get("BLOCKS_TEST_DB", "blocks_test")
ADMIN_DSN = os.environ.get(
    "BLOCKS_ADMIN_DSN", "host=postgres port=5432 user=blocks password=blocks-dev dbname=blocks"
)
MIGRATOR_PASSWORD = os.environ.get("BLOCKS_DB_MIGRATOR_PASSWORD", "blocks-dev-migrator")
APP_PASSWORD = os.environ.get("BLOCKS_DB_APP_PASSWORD", "blocks-dev-app")
DEV_SECRET = "test-signing-key-not-a-secret-32b+"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """A disposable database, rebuilt from migrations once per session."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{TEST_DB}" with (force)')
        conn.execute(f'create database "{TEST_DB}"')

    admin_test_dsn = _swap_dbname(ADMIN_DSN, TEST_DB)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "dev_db.py"), "--admin-dsn", admin_test_dsn],
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
