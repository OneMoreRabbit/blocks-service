"""Health, readiness, and the isolation self-check readiness performs."""

from __future__ import annotations

import pytest

from blocks_service.db import Database, RlsBypassError


async def test_liveness_does_not_depend_on_the_database(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_database_and_enforced_isolation(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["tenant_isolation"] == "enforced"


async def test_runtime_role_cannot_bypass_rls(db: Database):
    await db.assert_cannot_bypass_rls()


async def test_readiness_fails_when_the_role_could_bypass_rls(settings):
    """The self-check must actually fail for a privileged role, not just pass.

    A check that cannot fail is indistinguishable from one that is not running,
    so this asserts the negative case against the superuser role directly.
    """
    admin = Database(settings.dsn(user="blocks", password="blocks-dev"))
    await admin.open()
    try:
        with pytest.raises(RlsBypassError):
            await admin.assert_cannot_bypass_rls()
    finally:
        await admin.close()
