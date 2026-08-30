"""Every error the API can produce uses one envelope and one set of codes.

The contract tells clients to branch on `error.code` and lists the codes. A
status or shape the contract does not describe is a defect even when the request
was correctly rejected, because a client cannot recognise it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from blocks_service.app import create_app
from blocks_service.config import Settings
from blocks_service.errors import ErrorCode

DOCUMENTED_STATUSES = {400, 401, 403, 404, 409, 426}


async def _session(client, auth, subject: str) -> dict:
    return (await client.post("/v1/session", headers=auth(subject))).json()


def _assert_envelope(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, f"unexpected top-level keys: {set(body)}"
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] in {c.value for c in ErrorCode}
    assert response.status_code in DOCUMENTED_STATUSES
    return body["error"]


async def test_an_invalid_body_is_400_invalid_request_not_422(client, auth):
    """The defect this test exists for: FastAPI's default 422 is not in the contract."""
    alice = await _session(client, auth, "alice")
    response = await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": ""},
    )
    assert response.status_code == 400
    assert _assert_envelope(response)["code"] == "invalid_request"


async def test_a_missing_body_field_is_400(client, auth):
    alice = await _session(client, auth, "alice")
    response = await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={},
    )
    assert response.status_code == 400
    assert _assert_envelope(response)["code"] == "invalid_request"


async def test_a_malformed_tenant_id_is_400(client, auth):
    await _session(client, auth, "alice")
    response = await client.get("/v1/tenants/not-a-uuid/projects", headers=auth("alice"))
    assert response.status_code == 400
    assert _assert_envelope(response)["code"] == "invalid_request"


async def test_a_validation_message_does_not_echo_the_submitted_value(client, auth):
    """Messages reach logs and client-side reports; a rejected body is untrusted."""
    alice = await _session(client, auth, "alice")
    secret = "s3cret-value-that-must-not-be-echoed"
    response = await client.post(
        f"/v1/tenants/{alice['personal_tenant_id']}/projects",
        headers=auth("alice"),
        json={"name": secret},
    )
    if response.status_code == 400:
        assert secret not in response.text
    # A 201 is also a correct outcome here; the assertion is about the failure path.


async def test_a_routing_404_uses_the_same_envelope(client):
    response = await client.get("/v1/there-is-no-such-path")
    assert response.status_code == 404
    assert _assert_envelope(response)["code"] == "not_found"


async def test_no_endpoint_returns_an_undocumented_status(client, auth):
    """Sweep the failure paths a client will actually hit."""
    alice = await _session(client, auth, "alice")
    tenant = alice["personal_tenant_id"]
    await _session(client, auth, "bob")

    responses = [
        await client.post("/v1/session"),
        await client.post("/v1/session", headers={"Authorization": "Bearer nope"}),
        await client.get(f"/v1/tenants/{tenant}/projects"),
        await client.get(f"/v1/tenants/{tenant}/projects", headers=auth("bob")),
        await client.get(f"/v1/tenants/{uuid4()}/projects", headers=auth("alice")),
        await client.post(f"/v1/tenants/{tenant}/projects", headers=auth("alice"), json={}),
        await client.get("/v1/tenants/not-a-uuid/projects", headers=auth("alice")),
    ]
    for response in responses:
        assert response.status_code != 422, "422 is not a documented status"
        _assert_envelope(response)


def test_the_published_openapi_documents_400_and_never_422():
    """The generated artefact is what clients read; it must not advertise 422."""
    spec = create_app(
        Settings(db_app_password="unused", dev_token_secret="unused-for-schema-only")
    ).openapi()

    statuses = {
        code
        for path in spec["paths"].values()
        for operation in path.values()
        for code in operation.get("responses", {})
    }
    assert "422" not in statuses
    assert "400" in statuses

    error_schema = spec["components"]["schemas"]["Error"]
    assert set(error_schema["properties"]["error"]["properties"]["code"]["enum"]) == {
        c.value for c in ErrorCode
    }
