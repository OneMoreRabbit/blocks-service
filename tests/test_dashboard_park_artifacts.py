"""Describe the enabled Park projection while preserving frozen API 0.8."""

from __future__ import annotations

import hashlib
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


def test_frozen_0_8_export_omits_park_shape(tmp_path):
    subprocess.run(
        [
            sys.executable, "tools/export_contract.py", "--out", str(tmp_path),
            "--publication-version", "0.8",
        ],
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


def test_default_export_describes_enabled_park_and_only_park(tmp_path):
    subprocess.run(
        [sys.executable, "tools/export_contract.py", "--out", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    artifact = tmp_path / "blocks-service-api-openapi-v0_10.json"
    spec = json.loads(artifact.read_text())
    operation = spec["paths"][PATH]["get"]
    assert any(parameter["name"] == "include" for parameter in operation["parameters"])
    assert {"NewestParkingSummaryOut", "ProjectWithParkSummaryOut"} <= set(
        spec["components"]["schemas"]
    )
    assert not any("follow" in path.lower() for path in spec["paths"])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == (
        "6b42b3f77fb32a125d52f5df9e9c6e393674b941f5c5feeac239a2771c6131b7"
    )
