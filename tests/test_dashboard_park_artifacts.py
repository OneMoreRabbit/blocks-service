"""Keep the unactivated Park projection out of frozen wire API 0.8."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

PATH = "/v1/tenants/{tenant_id}/projects"


def test_proposed_park_openapi_matches_candidate_source(tmp_path):
    proposed = pathlib.Path(
        "contracts/proposed/blocks-service-dashboard-park-summary-openapi-v0_1.json"
    )
    generated = tmp_path / proposed.name
    subprocess.run(
        [
            sys.executable,
            "tools/export_dashboard_park_summary_proposal.py",
            "--out",
            str(generated),
        ],
        check=True,
        capture_output=True,
    )
    spec = json.loads(proposed.read_text())
    assert spec == json.loads(generated.read_text())
    operation = spec["paths"][PATH]["get"]
    assert any(parameter["name"] == "include" for parameter in operation["parameters"])


def test_active_wire_export_omits_proposed_park_shape(tmp_path):
    subprocess.run(
        [sys.executable, "tools/export_contract.py", "--out", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    active = json.loads(
        (tmp_path / "blocks-service-api-openapi-v0_8.json").read_text()
    )
    operation = active["paths"][PATH]["get"]
    assert not any(parameter["name"] == "include" for parameter in operation["parameters"])
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "items": {"$ref": "#/components/schemas/ProjectOut"},
        "title": "Response List Projects V1 Tenants  Tenant Id  Projects Get",
        "type": "array",
    }
    assert "NewestParkingSummaryOut" not in active["components"]["schemas"]
    assert "ProjectWithParkSummaryOut" not in active["components"]["schemas"]
