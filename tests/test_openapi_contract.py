"""Automated audits of the published OpenAPI document.

Every check here exists because the document once said something the service did
not do, and a generated client would have believed it. These run in CI so the
artefact cannot drift back.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from blocks_service import events
from blocks_service.app import UNAUTHENTICATED_PATHS, create_app
from blocks_service.config import Settings


@pytest.fixture(scope="module")
def spec() -> dict:
    return create_app(
        Settings(db_app_password="unused", dev_token_secret="unused-for-schema-only")
    ).openapi()


def _operations(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            yield path, method, op


def test_every_templated_path_parameter_is_declared(spec):
    """A path variable the document does not declare makes it invalid to generate.

    Three operations were missing `project_id`. This is the audit that would
    have caught them.
    """
    missing = []
    for path, method, op in _operations(spec):
        templated = set(re.findall(r"{(\w+)}", path))
        declared = {p["name"] for p in op.get("parameters", []) if p.get("in") == "path"}
        if templated - declared:
            missing.append(f"{method.upper()} {path}: {sorted(templated - declared)}")
    assert not missing


def test_authentication_is_declared_and_only_probes_are_exempt(spec):
    """A generated client with no bearer mechanism cannot call anything."""
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert spec["security"] == [{"bearerAuth": []}]

    exempt = {
        path for path, _, op in _operations(spec) if op.get("security") == []
    }
    assert exempt == set(UNAUTHENTICATED_PATHS)


def test_session_bootstrap_still_requires_a_bearer_token(spec):
    """It is where an identity token is exchanged, not where one stops mattering."""
    assert "security" not in spec["paths"]["/v1/session"]["post"]


def test_contract_headers_are_declared_where_they_apply(spec):
    """Prose-only headers are invisible to a generator."""
    by_op = {
        f"{m.upper()} {p}": {
            h["name"]: h for h in op.get("parameters", []) if h.get("in") == "header"
        }
        for p, m, op in _operations(spec)
    }

    transitions = [
        "POST /v1/tenants/{tenant_id}/sessions/{session_id}/park",
        "POST /v1/tenants/{tenant_id}/sessions/{session_id}/resume",
        "POST /v1/tenants/{tenant_id}/sessions/{session_id}/close",
        "PATCH /v1/tenants/{tenant_id}/projects/{project_id}/map/{item_id}",
    ]
    for op in transitions:
        assert "If-Match" in by_op[op], op
        assert by_op[op]["If-Match"]["required"] is True, op

    idempotent = [
        "POST /v1/tenants/{tenant_id}/sessions",
        "POST /v1/tenants/{tenant_id}/blocks",
    ]
    for op in idempotent:
        assert "Idempotency-Key" in by_op[op], op
        assert by_op[op]["Idempotency-Key"]["required"] is False, op


def test_idempotent_replay_is_declared_as_a_response_header(spec):
    """A replay is indistinguishable from a first response without it."""
    op = spec["paths"]["/v1/tenants/{tenant_id}/sessions"]["post"]
    assert "Idempotent-Replay" in op["responses"]["201"]["headers"]


def test_pickup_publishes_exactly_one_selector(spec):
    """`anyOf` would permit both. The contract means one."""
    body = spec["paths"]["/v1/tenants/{tenant_id}/sessions"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert "oneOf" in schema
    assert "anyOf" not in schema
    assert len(schema["oneOf"]) == 2


def test_the_change_feed_publishes_the_envelope_type(spec):
    """`array<object>` tells a generator nothing about what it is receiving."""
    schemas = spec["components"]["schemas"]
    assert "EventEnvelopeOut" in schemas
    props = schemas["EventEnvelopeOut"]["properties"]
    assert {"eventId", "eventType", "eventVersion", "tenantId", "occurredAt", "payload"} <= set(props)


def test_no_success_response_is_untyped_and_no_422_is_advertised(spec):
    untyped, has_422 = [], False
    for path, method, op in _operations(spec):
        for code, resp in op.get("responses", {}).items():
            if code == "422":
                has_422 = True
            if code.startswith("2") and code != "204":
                if not resp.get("content", {}).get("application/json", {}).get("schema"):
                    untyped.append(f"{method.upper()} {path} {code}")
    assert not untyped
    assert not has_422


def test_the_document_version_matches_the_contract(spec):
    assert spec["info"]["version"] == "0.2.0"


# --------------------------------------------------------------------------
# Event payloads must not carry user-authored text
# --------------------------------------------------------------------------

#: `project.created` carries `name`, and it is in the ACCEPTED 0.1 contract.
#: Removing it is a breaking change to an artefact a consumer may have generated
#: from, so it is raised for decision rather than changed unilaterally. Listed
#: here so the exception is explicit and cannot quietly grow.
KNOWN_TEXT_CARRYING_EVENTS = {"project.created"}

#: Field names whose values are typed by a human.
TEXT_FIELDS = {
    "name", "title", "coreOutcome", "fullOutcome", "polishOutcome",
    "whereIAm", "nextPhysicalAction", "mustNotForget", "label",
}


@pytest.mark.parametrize("event_type", sorted(events.EVENT_VERSIONS))
def test_event_payloads_carry_identifiers_not_content(event_type):
    """Payloads travel through transports and sinks this service does not control."""
    if event_type in KNOWN_TEXT_CARRYING_EVENTS:
        pytest.skip(f"{event_type} predates this rule; raised for decision")
    schema = events.load_schema(event_type)
    offending = set(schema.get("properties", {})) & TEXT_FIELDS
    assert not offending, f"{event_type} carries user text: {sorted(offending)}"
