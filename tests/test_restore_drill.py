"""The restore drill, and proof that it fails when a restore is silently broken.

The deployment contract claims restore is proven rather than asserted. These
tests are what makes that claim true, and the negative ones are the important
half: a drill that cannot fail is indistinguishable from a drill not running.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from blocks_service.app import create_app

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = "blocks_service_test_drill"


def _load_drill():
    spec = importlib.util.spec_from_file_location(
        "restore_drill", ROOT / "tools" / "restore_drill.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drill = _load_drill()

#: The drill shells out to real PostgreSQL client binaries, deliberately -- a
#: home-grown dump would not exercise what an operator's backup actually does.
#: They are not yet part of the seat image, so skip with a reason a reader can
#: act on rather than failing with a confusing FileNotFoundError.
requires_pg_client = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="pg_dump/pg_restore not installed; see the seat-tooling need in the Atlas vault",
)

def _dump_role_can_bypass_rls() -> bool:
    from tests.conftest import ADMIN_DSN

    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=5, autocommit=True) as conn:
            row = conn.execute(
                "select rolsuper or rolbypassrls as ok from pg_roles where rolname = current_user"
            ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


#: pg_dump cannot dump FORCE RLS tables as a role subject to those policies, so
#: the drill needs a role exempt from them. Since the estate rotated its
#: superuser away there is no such role reachable from this seat -- see the
#: backup-role need in the Atlas vault. Skipped rather than silently weakened:
#: the alternative, --enable-row-security, exits 0 and dumps no tenant rows.
requires_dump_capable_role = pytest.mark.skipif(
    not _dump_role_can_bypass_rls(),
    reason="dumping role cannot bypass RLS; pg_dump cannot dump this schema — see the backup-role need",
)

pytestmark = [requires_pg_client, requires_dump_capable_role]


@pytest.fixture(autouse=True)
def drop_scratch() -> Iterator[None]:
    from tests.conftest import ADMIN_DSN

    yield
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{SCRATCH}" with (force)')


def _run_drill(source_db: str, *, keep: bool) -> subprocess.CompletedProcess:
    from tests.conftest import ADMIN_DSN, APP_PASSWORD

    cmd = [
        sys.executable, str(ROOT / "tools" / "restore_drill.py"),
        "--admin-dsn", ADMIN_DSN,
        "--source-db", source_db,
        "--scratch-db", SCRATCH,
        "--app-password", APP_PASSWORD,
    ]
    if keep:
        cmd.append("--keep")
    return subprocess.run(cmd, capture_output=True, text=True)


async def _seed(client, auth) -> None:
    """Two tenants with one project each -- the shape the cross-tenant probe needs."""
    for who in ("alice", "bob"):
        session = (await client.post("/v1/session", headers=auth(who))).json()
        await client.post(
            f"/v1/tenants/{session['personal_tenant_id']}/projects",
            headers=auth(who),
            json={"name": f"{who} only"},
        )


async def test_the_drill_passes_on_a_seeded_database(client, auth, settings):
    await _seed(client, auth)
    result = _run_drill(settings.db_name, keep=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RLS enabled AND forced on memberships, projects, outbox" in result.stdout
    assert "cross-tenant probe: two tenants saw only their own rows" in result.stdout
    assert result.stdout.strip().endswith("PASSED")


async def test_the_service_passes_readiness_against_the_restored_database(
    client, auth, settings
):
    """The deployment contract's actual wording, executed."""
    await _seed(client, auth)
    assert _run_drill(settings.db_name, keep=True).returncode == 0

    restored = settings.model_copy(update={"db_name": SCRATCH})
    app = create_app(restored)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://restored.test"
        ) as c:
            ready = await c.get("/health/ready")

    assert ready.status_code == 200
    assert ready.json()["checks"]["tenant_isolation"] == "enforced"


async def test_the_restored_database_still_serves_its_tenants(client, auth, settings):
    """A restore that isolates perfectly but returns nothing is also a failure."""
    await _seed(client, auth)
    assert _run_drill(settings.db_name, keep=True).returncode == 0

    restored = settings.model_copy(update={"db_name": SCRATCH})
    app = create_app(restored)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://restored.test"
        ) as c:
            session = (await c.post("/v1/session", headers=auth("alice"))).json()
            listed = await c.get(
                f"/v1/tenants/{session['personal_tenant_id']}/projects", headers=auth("alice")
            )

    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["alice only"]


async def test_the_drill_fails_when_rls_did_not_survive_the_restore(client, auth, settings):
    """The silent failure the drill exists for.

    Every row comes back, the schema looks right, readiness would pass -- and
    every tenant can see every other tenant's data.
    """
    from tests.conftest import ADMIN_DSN, APP_PASSWORD

    await _seed(client, auth)
    assert _run_drill(settings.db_name, keep=True).returncode == 0

    scratch_admin = drill.swap_db(ADMIN_DSN, SCRATCH)
    with psycopg.connect(scratch_admin, autocommit=True) as conn:
        conn.execute("alter table blocks.projects disable row level security")

    with pytest.raises(drill.DrillFailure) as exc:
        drill.verify_isolation(drill.as_role(scratch_admin, "blocks_app", APP_PASSWORD))
    assert "row-level security did not survive" in str(exc.value)


async def test_the_drill_fails_when_force_was_lost_but_rls_remained(client, auth, settings):
    """ENABLE without FORCE exempts the table owner -- subtler, equally total."""
    from tests.conftest import ADMIN_DSN

    await _seed(client, auth)
    assert _run_drill(settings.db_name, keep=True).returncode == 0

    scratch_admin = drill.swap_db(ADMIN_DSN, SCRATCH)
    with psycopg.connect(scratch_admin, autocommit=True) as conn:
        conn.execute("alter table blocks.projects no force row level security")

    tampered = drill.snapshot(scratch_admin)
    with pytest.raises(drill.DrillFailure) as exc:
        drill.compare(tampered, tampered)
    assert "force=False" in str(exc.value)


async def test_the_drill_fails_when_a_policy_was_lost(client, auth, settings):
    """Losing a policy denies everything rather than leaking -- loud, but still wrong."""
    from tests.conftest import ADMIN_DSN

    await _seed(client, auth)
    assert _run_drill(settings.db_name, keep=True).returncode == 0

    scratch_admin = drill.swap_db(ADMIN_DSN, SCRATCH)
    before = drill.snapshot(scratch_admin)
    with psycopg.connect(scratch_admin, autocommit=True) as conn:
        conn.execute("drop policy projects_tenant_isolation on blocks.projects")

    with pytest.raises(drill.DrillFailure) as exc:
        drill.compare(before, drill.snapshot(scratch_admin))
    assert "policies differ" in str(exc.value)
