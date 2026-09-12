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
    assert spec["info"]["version"] == "0.5.0"


# --------------------------------------------------------------------------
# Event payloads must not carry user-authored text
# --------------------------------------------------------------------------

#: No exceptions. `project.created` was the last one: it carried `name` in an
#: accepted-0.1 payload, and the architecture ruling of 2026-09-04 was to publish
#: an identifier-only 0.2 rather than mutate 0.1. The skip that held its place is
#: now an assertion, which is the point of the ruling -- an exception nobody can
#: see stops being an exception and becomes the rule.
KNOWN_TEXT_CARRYING_EVENTS: set[str] = set()

#: Field names whose values are typed by a human.
TEXT_FIELDS = {
    "name", "title", "coreOutcome", "fullOutcome", "polishOutcome",
    "whereIAm", "nextPhysicalAction", "mustNotForget", "label",
}


@pytest.mark.parametrize("event_type", sorted(events.EVENT_VERSIONS))
def test_event_payloads_carry_identifiers_not_content(event_type):
    """Payloads travel through transports and sinks this service does not control.

    Every currently-emitted event, with no exemptions. The list above is empty and
    should stay that way: a named exception is a rule with a hole in it, and this
    one took three weeks and an architecture ruling to close.
    """
    assert not KNOWN_TEXT_CARRYING_EVENTS, "the exemption list should stay empty"
    schema = events.load_schema(event_type)
    offending = set(schema.get("properties", {})) & TEXT_FIELDS
    assert not offending, f"{event_type} carries user text: {sorted(offending)}"


def test_project_created_is_now_identifier_only():
    """The ruling, asserted directly rather than only through the sweep."""
    assert events.EVENT_VERSIONS["project.created"] == "0.2"
    assert set(events.load_schema("project.created")["properties"]) == {"projectId"}


def test_the_superseded_project_created_schema_is_preserved():
    """0.1 rows are already in the outbox and must stay readable.

    Deleting the old schema would make correct history unvalidatable — which is
    the whole reason the version is stamped on the row rather than assumed from
    the deployment.
    """
    assert events.known_versions("project.created") == ["0.1", "0.2"]
    historical = events.load_schema("project.created", "0.1")
    assert "name" in historical["properties"]


def test_a_historical_payload_validates_against_its_own_stamped_version():
    """Validating old history against the new shape would call it a defect."""
    import jsonschema

    old_payload = {"projectId": "6f1e0a1e-0000-4000-8000-000000000000", "name": "Kitchen"}
    jsonschema.validate(old_payload, events.load_schema("project.created", "0.1"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(old_payload, events.load_schema("project.created", "0.2"))


def test_no_schema_name_is_module_mangled(spec):
    """A model-name collision renames BOTH sides, including accepted ones.

    `SessionOut` is the bootstrap response published in accepted 0.1. When 0.2
    reused the name for work sessions, FastAPI disambiguated by mangling both
    into module-qualified names — silently renaming an accepted schema out from
    under any client generating against 0.2.
    """
    mangled = [name for name in spec["components"]["schemas"] if "__" in name]
    assert not mangled, f"model-name collision mangled: {mangled}"


def test_the_accepted_0_1_schema_names_survive_in_0_2(spec):
    """0.2 is additive; a name a consumer already generated from must not move."""
    import json
    import pathlib

    provides = pathlib.Path(".atlas/components/blocks-service/docs/provides")
    # Superseded contracts and the sidecars only they declare are filed under
    # archive/. Archiving is not withdrawal: these are still the bytes a 0.1
    # consumer generated from, so the assertion must follow the file rather than
    # quietly skip and stop protecting those names.
    published = provides / "blocks-service-api-openapi-v0_1.json"
    if not published.exists():
        published = provides / "archive" / "blocks-service-api-openapi-v0_1.json"
    if not published.exists():
        pytest.skip("0.1 artefact not present in this checkout")
    accepted = set(json.loads(published.read_text())["components"]["schemas"])
    # FastAPI's own validation schemas are not part of the contract.
    accepted -= {"HTTPValidationError", "ValidationError"}
    assert accepted <= set(spec["components"]["schemas"])


def test_the_reentry_result_is_required_and_nullable(spec):
    """No-result must be nulls, never `{}` or omitted keys."""
    reentry = spec["components"]["schemas"]["ReentryOut"]
    assert set(reentry["required"]) == {"session", "parking"}
    for field in ("session", "parking"):
        assert any(
            option.get("type") == "null" for option in reentry["properties"][field]["anyOf"]
        ), field


def test_block_registration_publishes_its_two_forms_and_its_replay(spec):
    """The contract-visible behaviour a generated client cannot infer privately.

    Architecture's 0.5 review blocked on exactly this: the handler returned a
    resource replay the document did not declare, so an Android generator could
    only reach the prose contract by guessing.
    """
    op = spec["paths"]["/v1/tenants/{tenant_id}/blocks"]["post"]

    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert "oneOf" in body and "anyOf" not in body, (
        "the two registration forms are a closed choice, never a merge"
    )
    assert len(body["oneOf"]) == 2

    created = op["responses"]["201"]["content"]["application/json"]["schema"]
    assert "oneOf" in created and "anyOf" not in created

    replay = op["responses"]["200"]
    assert replay["content"]["application/json"]["schema"]["$ref"].endswith("ReplayedBlockOut")
    assert set(replay["headers"]) >= {"Idempotent-Replay", "Idempotent-Replay-Source"}
    assert replay["headers"]["Idempotent-Replay-Source"]["schema"]["enum"] == ["resource"]


def test_the_client_form_never_publishes_a_token_in_a_result(spec):
    """Search the published models, not the prose."""
    schemas = spec["components"]["schemas"]
    assert "tag_token" not in schemas["ClientRegisteredBlockOut"]["properties"]
    assert "tag_token" not in schemas["ReplayedBlockOut"]["properties"]
    assert "tag_token" in schemas["RegisteredBlockOut"]["properties"], (
        "the 0.4 server-minted form keeps its one-time result"
    )


def test_the_local_first_label_length_is_honest(spec):
    schemas = spec["components"]["schemas"]
    assert schemas["ClientBlockIn"]["properties"]["label"]["maxLength"] == 200
    assert schemas["LegacyBlockIn"]["properties"]["label"]["maxLength"] == 120
