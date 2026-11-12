"""Flaky-test detection from test history.

A test is flaky when the *same commit* produced both a pass and a fail: the
code did not change, so the result depends on timing, ordering or the network.
Results come from JUnit XML (pytest --junitxml, Jest, Go, Maven all emit it).

Flip rate counts pass<->fail changes from one run to the next, so it needs the
runs in time order. Each result carries a run_number (e.g. GitHub's run number)
and results are sorted by it first; results with equal numbers keep the order
they were given in.
"""

import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class TestResult:
    test_id: str
    commit: str
    passed: bool
    run_id: str = ""
    run_number: int = 0  # position in time: higher = later run


def parse_junit(xml_text: str, commit: str, run_id: str = "", run_number: int = 0) -> list[TestResult]:
    """Read every <testcase>; it failed if it holds <failure> or <error>. Skips are ignored."""
    root = ET.fromstring(xml_text)
    results = []
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        failed = case.find("failure") is not None or case.find("error") is not None
        results.append(TestResult(name, commit, not failed, run_id, run_number))
    return results


def find_flaky(results: list[TestResult], min_runs: int = 2) -> list[dict]:
    by_test: dict[str, list[TestResult]] = defaultdict(list)
    for r in results:
        by_test[r.test_id].append(r)

    report = []
    for test_id, runs in by_test.items():
        if len(runs) < min_runs:
            continue
        runs = sorted(runs, key=lambda r: r.run_number)  # oldest first; sorted() is stable
        outcomes_by_commit: dict[str, set[bool]] = defaultdict(set)
        for r in runs:
            outcomes_by_commit[r.commit].add(r.passed)
        mixed = [c for c, outcomes in outcomes_by_commit.items() if len(outcomes) == 2]
        if not mixed:
            continue
        failures = sum(not r.passed for r in runs)
        flips = sum(a.passed != b.passed for a, b in zip(runs, runs[1:]))
        report.append({
            "test_id": test_id,
            "runs": len(runs),
            "fail_rate": round(failures / len(runs), 2),
            "flip_rate": round(flips / (len(runs) - 1), 2),
            "commits_with_mixed_results": mixed,
        })
    return sorted(report, key=lambda r: (r["flip_rate"], r["fail_rate"]), reverse=True)
