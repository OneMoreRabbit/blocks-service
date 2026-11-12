"""Exercise the UC-04 gate with real pytest JUnit reports, without a database."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


GATE = Path(__file__).resolve().parents[1] / "scripts/check_uc04_pytest_result.py"


def _pytest_report(tmp_path: Path, source: str) -> tuple[Path, int, str]:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(source)
    report = tmp_path / "result.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "xfail_strict=true",
         f"--junitxml={report}", str(test_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return report, result.returncode, result.stdout + result.stderr


def _gate(report: Path, pytest_exit: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(report), str(pytest_exit)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "source", "pytest_exit", "gate_exit"),
    [
        ("normal success", "def test_ok():\n    assert True\n", 0, 0),
        (
            "passing subtests",
            "import unittest\n"
            "class TestItems(unittest.TestCase):\n"
            "    def test_items(self):\n"
            "        for item in range(3):\n"
            "            with self.subTest(item=item):\n"
            "                self.assertGreaterEqual(item, 0)\n",
            0, 0,
        ),
        ("failure", "def test_fail():\n    assert False\n", 1, 1),
        (
            "error", "import pytest\n@pytest.fixture\ndef broken():\n"
            "    raise RuntimeError('setup error')\ndef test_error(broken):\n    pass\n",
            1, 1,
        ),
        ("skip", "import pytest\n@pytest.mark.skip(reason='skip')\ndef test_skip():\n    pass\n", 0, 1),
        (
            "xfail", "import pytest\n@pytest.mark.xfail(reason='expected')\n"
            "def test_xfail():\n    assert False\n",
            0, 1,
        ),
        (
            "xpass", "import pytest\n@pytest.mark.xfail(reason='unexpected')\n"
            "def test_xpass():\n    assert True\n",
            1, 1,
        ),
        ("zero tests", "# no tests\n", 5, 1),
    ],
)
def test_gate_outcomes(tmp_path, name, source, pytest_exit, gate_exit):
    report, actual_exit, output = _pytest_report(tmp_path, source)
    assert actual_exit == pytest_exit, f"{name}: {output}"
    assert report.exists(), f"{name}: {output}"
    result = _gate(report, 0 if name == "zero tests" else actual_exit)
    assert result.returncode == gate_exit, f"{name}: {result.stdout}{result.stderr}"
    if name == "passing subtests":
        assert "subtests passed" in output
        assert "4 JUnit tests/subtests" in result.stdout


def test_gate_rejects_missing_report(tmp_path):
    assert _gate(tmp_path / "absent.xml", 0).returncode == 1


def test_gate_preserves_nonzero_pytest_exit_even_with_passing_report(tmp_path):
    report, pytest_exit, output = _pytest_report(tmp_path, "def test_ok():\n    pass\n")
    assert pytest_exit == 0, output
    result = _gate(report, 7)
    assert result.returncode == 7
    assert "pytest exited 7" in result.stderr
