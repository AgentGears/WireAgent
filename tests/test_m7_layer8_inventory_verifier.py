"""Layer 8 — E-2 JUnit verifier negative controls (L8-EF06, rev 2).

Every negative control starts from ONE shared VALID baseline — 71 unique
mandatory identities, the three permitted skips with correct reasons, no
duplicates, no failures — and introduces EXACTLY ONE prohibited
condition. Each control asserts BOTH that the baseline passes AND that
the mutated document fails for the EXPECTED reason (matched against the
verifier's stderr), so a control can no longer pass for an unintended
violation the fixture smuggled in.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
import _l8_inventory as inv  # noqa: E402

ALL_NODES = {line.strip() for line in inv.MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()}
F90 = "tests/test_m7_layer6_ingress.py::test_F90_substituted_pathname_bytes_come_from_validated_handle"


def _xml(cases: list[ET.Element]) -> str:
    root = ET.Element("testsuite", {"name": "l8", "tests": str(len(cases))})
    for case in cases:
        root.append(case)
    return ET.tostring(root, encoding="unicode")


def _case(node: str, outcome: str | None, message: str = "") -> ET.Element:
    path, name = node.split("::")
    case = ET.Element("testcase", {"classname": path.replace("/", ".")[:-3], "name": name})
    if outcome == "skipped":
        ET.SubElement(case, "skipped", {"message": message})
    elif outcome == "failure":
        ET.SubElement(case, "failure", {"message": message})
    elif outcome == "error":
        ET.SubElement(case, "error", {"message": message})
    return case


def _baseline_cases() -> list[ET.Element]:
    """The ONE valid shape: exactly one outcome per identity; the three
    permitted skips carry their correct reasons; everything else passes."""
    return [
        _case(n, "skipped", inv.EXPECTED_SKIPS[n]) if n in inv.EXPECTED_SKIPS else _case(n, None)
        for n in sorted(ALL_NODES)
    ]


def _write(tmp_path: Path, name: str, cases: list[ET.Element]) -> Path:
    p = tmp_path / f"{name}.xml"
    p.write_text(_xml(cases), encoding="utf-8")
    return p


def _assert_rejects(tmp_path: Path, name: str, cases: list[ET.Element], expected_reason: str, capsys) -> None:
    p = _write(tmp_path, name, cases)
    rc = inv.verify_run(str(p))
    err = capsys.readouterr().err
    assert rc == 1, "verifier accepted a document it must reject: " + err
    assert expected_reason in err, "expected reason not reported: " + err


def test_baseline_is_valid(tmp_path: Path, capsys) -> None:
    """The shared fixture passes untouched — every negative control's
    precondition, asserted once here rather than assumed."""
    p = _write(tmp_path, "baseline", _baseline_cases())
    assert inv.verify_run(str(p)) == 0, capsys.readouterr().err


def test_verifier_rejects_missing_scenario(tmp_path: Path, capsys) -> None:
    cases = [
        c for c in _baseline_cases()
        if not (c.get("name") or "").startswith("test_T34_pre_reserved")
    ]
    assert len(cases) == len(_baseline_cases()) - 1
    _assert_rejects(tmp_path, "missing", cases, "missing/deselected", capsys)


def test_verifier_rejects_the_f90_skip(tmp_path: Path, capsys) -> None:
    f90_name = "test_F90_substituted_pathname_bytes_come_from_validated_handle"
    cases = [
        _case(F90, "skipped", "symlink creation failed")
        if c.get("name", "") == f90_name
        else c
        for c in _baseline_cases()
    ]
    _assert_rejects(tmp_path, "f90skip", cases, "skipped", capsys)
    # And specifically named: the rejection must name F90, not any other skip.
    p = _write(tmp_path, "f90skip2", cases)
    inv.verify_run(str(p))
    assert "test_F90_substituted" in capsys.readouterr().err


def test_verifier_rejects_wrong_skip_reason(tmp_path: Path, capsys) -> None:
    wrong = "tests/test_m7_layer7_supported_faults.py::test_T65_endpoint_rendezvous_loss_is_not_ownership_loss"
    t65_name = "test_T65_endpoint_rendezvous_loss_is_not_ownership_loss"
    cases = [
        _case(wrong, "skipped", "an unrelated reason")
        if c.get("name", "") == t65_name
        else c
        for c in _baseline_cases()
    ]
    _assert_rejects(tmp_path, "wrongreason", cases, "WRONG reason", capsys)


def test_verifier_rejects_failed_mandatory_test(tmp_path: Path, capsys) -> None:
    first = sorted(ALL_NODES)[0]
    first_name = first.split("::")[1]
    cases = [
        _case(first, "failure", "boom") if c.get("name") == first_name else c
        for c in _baseline_cases()
    ]
    _assert_rejects(tmp_path, "failed", cases, "failure:", capsys)


def test_verifier_rejects_duplicate_failure_then_pass(tmp_path: Path, capsys) -> None:
    first = sorted(ALL_NODES)[0]
    cases = _baseline_cases() + [_case(first, "failure", "boom"), _case(first, None)]
    _assert_rejects(tmp_path, "dup1", cases, "duplicate identity", capsys)


def test_verifier_rejects_duplicate_skip_then_pass(tmp_path: Path, capsys) -> None:
    skip_node = next(iter(inv.EXPECTED_SKIPS))
    cases = _baseline_cases() + [_case(skip_node, None)]
    _assert_rejects(tmp_path, "dup2", cases, "duplicate identity", capsys)


def test_verifier_rejects_malformed_xml(tmp_path: Path, capsys) -> None:
    p = tmp_path / "bad.xml"
    p.write_text("<testsuite><testcase>", encoding="utf-8")
    assert inv.verify_run(str(p)) == 1
    assert "malformed junit xml" in capsys.readouterr().err
