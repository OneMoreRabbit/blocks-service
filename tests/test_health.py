"""Health, readiness, and the isolation self-check readiness performs."""

from __future__ import annotations

import pytest

from blocks_service.db import Database, RlsBypassError, assert_role_cannot_bypass


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


@pytest.mark.parametrize(
    "row",
    [
        {"rolsuper": True, "rolbypassrls": False},
        {"rolsuper": False, "rolbypassrls": True},
        {"rolsuper": True, "rolbypassrls": True},
        None,
    ],
)
def test_the_readiness_check_rejects_a_role_that_could_bypass_rls(row):
    """The self-check must actually fail, not merely pass on a safe role.

    This used to connect as the cluster superuser. That credential was rotated
    away after this component reported that client seats could reach the database
    with it, so no role remains that can exercise the failure path live -- which
    is the desired end state and would otherwise have left the guard's negative
    case untested. The decision is asserted directly instead.
    """
    with pytest.raises(RlsBypassError):
        assert_role_cannot_bypass(row)


def test_the_readiness_check_accepts_a_constrained_role():
    assert_role_cannot_bypass({"rolsuper": False, "rolbypassrls": False})
