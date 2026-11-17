"""Keep the candidate Follow artifact separate from frozen wire API 0.8."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys


def test_proposed_follow_openapi_matches_candidate_source(tmp_path):
    proposed = pathlib.Path("contracts/proposed/blocks-service-follow-openapi-v0_1.json")
    generated = tmp_path / proposed.name
    subprocess.run(
        [sys.executable, "tools/export_follow_proposal.py", "--out", str(generated)],
        check=True,
        capture_output=True,
    )
    spec = json.loads(proposed.read_text())
    assert spec == json.loads(generated.read_text())
    assert any("/follow/selection" in path for path in spec["paths"])


def test_active_wire_export_omits_proposed_follow(tmp_path):
    subprocess.run(
        [sys.executable, "tools/export_contract.py", "--out", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    active = json.loads(
        (tmp_path / "blocks-service-api-openapi-v0_8.json").read_text()
    )
    assert not any("/follow/selection" in path for path in active["paths"])
    assert not any(name.startswith("Follow") for name in active["components"]["schemas"])
