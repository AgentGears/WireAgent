"""Layer 8 — E-2 expected-test-inventory verification (JUnit-XML based).

L8-EF01 correction: the first implementation parsed the pytest CONSOLE
summary, whose Windows form (backslash paths + line numbers, no ::node id)
never matched the regex — it reported "0 skips" over four real skips, a
false qualification statement. This version consumes STRUCTURED evidence
(pytest --junitxml) and fails closed on: collection failure, a missing or
deselected mandatory scenario, a mandatory scenario that did not pass, an
unexpected skip, and a permitted skip whose node id or reason does not
match the ledger.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent
MANIFEST = HERE / "l8_windows_inventory.txt"
ROOT = HERE.parent

SELECTION = [
    "tests/test_m7_layer7_ownership.py",
    "tests/test_m7_layer7_crash_matrix.py",
    "tests/test_m7_layer7_t16_race.py",
    "tests/test_m7_layer7_supported_faults.py",
    "tests/test_m7_layer7_ipc_faults.py",
    "tests/test_m7_layer7_media_boundary.py",
    "tests/test_m7_layer7_browser_child.py",
    "tests/test_m7_layer7_harness_safety.py",
    "tests/test_m7_layer6_ingress.py",
    "tests/test_m7_layer8_native_gate.py",
]

# The ONLY skips permitted in the manifest set on the qualified Windows
# image. Each entry: node id -> required reason SUBSTRING (platform label).
# NOTE: the F90 substituted-pathname test is DELIBERATELY ABSENT — its skip
# (symlink privilege) is a NATIVE EVIDENCE GAP, not a platform boundary;
# the workflow enables Developer Mode so it executes, and any skip of it
# fails qualification.
EXPECTED_SKIPS = {
    "tests/test_m7_layer7_supported_faults.py::test_T65_endpoint_rendezvous_loss_is_not_ownership_loss":
        "POSIX-pathname fault",
    "tests/test_m7_layer7_harness_safety.py::test_kill_helper_terminates_only_under_matching_identity":
        "positive path needs /proc identity",
    "tests/test_m7_layer6_ingress.py::test_F88_injected_check_to_open_race_refused":
        "O_NOFOLLOW confinement is the POSIX mechanism",
}


def collected() -> set[str]:
    probe = subprocess.run(  # noqa: S603 - fixed interpreter + repo files
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *SELECTION],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    if probe.returncode != 0:  # L8-EF01: collection failure fails closed
        raise SystemExit(f"INVENTORY FAIL: collection exited {probe.returncode}: {probe.stderr[-400:]}")
    return {
        line.strip()
        for line in probe.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    }


def verify_collect() -> int:
    expected = {line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()}
    actual = collected()
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        print(f"INVENTORY FAIL: missing={sorted(missing)} extra={sorted(extra)}", file=sys.stderr)
        return 1
    print(f"INVENTORY OK: {len(expected)} expected scenarios all present, none extra.")
    return 0


def verify_run(xml_path: str) -> int:
    try:
        tree = ET.parse(xml_path)  # noqa: S320 - pytest's own structured output
    except ET.ParseError as exc:
        print(f"INVENTORY FAIL: malformed junit xml: {exc}", file=sys.stderr)
        return 1
    outcomes: dict[str, tuple[str, str]] = {}
    for case in tree.iter("testcase"):
        classname = case.get("classname", "").replace(".", "/")
        name = case.get("name", "")
        # Reconstruct the file::node id: classname mirrors the path without .py
        path = next((s for s in SELECTION if classname.endswith(s[:-3].replace(".", "/"))), None)
        if path is None:
            continue  # a non-mandatory companion test (e.g. the E-1 gate tests)
        node = f"{path}::{name}"
        child = list(case)
        outcome = (
            ("passed", "")
            if not child
            else (child[0].tag, child[0].get("message", "") or (child[0].text or "")[:200])
        )
        if node in outcomes:  # L8-EF06: duplicates are ambiguous evidence
            outcomes[node] = ("duplicate", f"{outcomes[node]} THEN {outcome}")
        else:
            outcomes[node] = outcome

    failures = []
    expected = {line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()}
    for node in sorted(expected):
        if node not in outcomes:
            failures.append(f"missing/deselected: {node}")
            continue
        kind, message = outcomes[node]
        if kind == "passed":
            continue
        if node in EXPECTED_SKIPS and kind == "skipped":
            if EXPECTED_SKIPS[node].lower() not in message.lower():
                failures.append(f"permitted skip with WRONG reason: {node} -> {message!r}")
            else:
                print(f"expected skip, labeled legitimate: {node} ({EXPECTED_SKIPS[node]})")
            continue
        label = "duplicate identity" if kind == "duplicate" else kind
        failures.append(f"{label}: {node} ({message[:120]})")
    for node, (kind, _msg) in sorted(outcomes.items()):
        if node not in expected:
            failures.append(f"unexpected outcome for non-mandatory test: {node} {kind}")
    if failures:
        for f in failures:
            print(f"INVENTORY FAIL: {f}", file=sys.stderr)
        return 1
    print(f"INVENTORY OK: all {len(expected)} mandatory scenarios executed; skips match the ledger.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("verify-collect", "verify-run"):
        print("usage: _l8_inventory.py verify-collect | verify-run XML", file=sys.stderr)
        raise SystemExit(2)
    if sys.argv[1] == "verify-collect":
        raise SystemExit(verify_collect())
    if len(sys.argv) < 3:
        print("verify-run requires the junit xml path", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(verify_run(sys.argv[2]))
