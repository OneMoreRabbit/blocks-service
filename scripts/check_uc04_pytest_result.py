"""Check the UC-04 full-suite JUnit result without parsing pytest prose."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_uc04_pytest_result.py REPORT.xml PYTEST_EXIT", file=sys.stderr)
        return 2

    report = Path(sys.argv[1])
    try:
        pytest_exit = int(sys.argv[2])
    except ValueError:
        print("FAIL: invalid pytest exit status", file=sys.stderr)
        return 2

    if not 0 <= pytest_exit <= 255:
        print("FAIL: pytest exit status is outside the process exit range", file=sys.stderr)
        return 2
    if pytest_exit:
        print(f"FAIL: pytest exited {pytest_exit}; full gate rejected", file=sys.stderr)
        return pytest_exit

    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"FAIL: missing or malformed pytest JUnit report: {exc}", file=sys.stderr)
        return 1

    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if root.tag not in {"testsuite", "testsuites"} or not suites:
        print("FAIL: pytest JUnit report has no test suite", file=sys.stderr)
        return 1

    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    try:
        for suite in suites:
            for key in totals:
                value = int(suite.attrib[key])
                if value < 0:
                    raise ValueError(f"negative {key}")
                totals[key] += value
    except (KeyError, ValueError) as exc:
        print(f"FAIL: invalid pytest JUnit counts: {exc}", file=sys.stderr)
        return 1

    if totals["tests"] == 0:
        print("FAIL: pytest JUnit report contains zero tests", file=sys.stderr)
        return 1
    if any(totals[key] for key in ("failures", "errors", "skipped")):
        print(f"FAIL: non-passing pytest JUnit outcomes: {totals}", file=sys.stderr)
        return 1

    print(f"FULL GATE: {totals['tests']} JUnit tests/subtests, zero failures/errors/skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
