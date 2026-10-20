"""Layer 8 — E-2 JUnit verifier negative controls (L8-EF04).

The inventory verifier is itself a qualification oracle: it must reject
missing scenarios, unexpected skips, wrong skip reasons, failed tests,
extra-scenario failures, and malformed XML — and accept the exact
legitimate shape (all mandatory scenarios passed + the three labeled
skips). These controls qualify the oracle independently of the
application tests, per the E-tranche re-review.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
import _l8_inventory as inv  # noqa: E402


def _xml(cases: list[ET.Element]) -> str:
    root = ET.Element("testsuite", {"name": "l8", "tests": str(len(cases))})
    for case in cases:
        root.append(case)
    return ET.tostring(root, encoding="unicode")


def _case(node: str, outcome: str | None, message: str = "") -> ET.Element:
    path, name = node.split("::")
    case = ET.Element(
        "testcase",
        {"classname": path.replace("/", ".")[:-3], "name": name},
    )
    if outcome == "skipped":
        ET.SubElement(case, "skipped", {"message": message})
    elif outcome == "failure":
        ET.SubElement(case, "failure", {"message": message})
    elif outcome == "error":
        ET.SubElement(case, "error", {"message": message})
    return case


ALL_NODES = {line.strip() for line in inv.MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()}
F90 = "tests/test_m7_layer6_ingress.py::test_F90_substituted_pathname_bytes_come_from_validated_handle"


def _good_xml(tmp_path: Path) -> Path:
    cases = [_case(n, None) for n in ALL_NODES]
    for node, reason in inv.EXPECTED_SKIPS.items():
        cases.append(_case(node, "skipped", f"SKIPPED [1] {node}: {reason}"))
    p = tmp_path / "good.xml"
    p.write_text(_xml(cases), encoding="utf-8")
    return p


def _mutate(tmp_path: Path, name: str, transform) -> Path:
    cases = [_case(n, None) for n in ALL_NODES]
    cases.append(_case(F90, None))
    for node, reason in inv.EXPECTED_SKIPS.items():
        cases.append(_case(node, "skipped", reason))
    cases = transform(cases)
    p = tmp_path / f"{name}.xml"
    p.write_text(_xml(cases), encoding="utf-8")
    return p


def test_verifier_accepts_the_legitimate_shape(tmp_path: Path) -> None:
    assert inv.verify_run(str(_good_xml(tmp_path))) == 0


def test_verifier_rejects_missing_scenario(tmp_path: Path) -> None:
    drop = "test_T34_pre_reserved_crash_fresh_confirmation_required"
    kept = [c for c in ALL_NODES if not c.endswith(drop)]
    cases = [_case(n, None) for n in kept]
    cases.append(_case(F90, None))
    for node, reason in inv.EXPECTED_SKIPS.items():
        cases.append(_case(node, "skipped", reason))
    p = tmp_path / "missing2.xml"
    p.write_text(_xml(cases), encoding="utf-8")
    assert inv.verify_run(str(p)) == 1


def test_verifier_rejects_the_f90_skip(tmp_path: Path) -> None:
    """The review's named case: an artificial JUnit carrying the F90 skip
    as an unexpected fourth skip must FAIL the verifier."""

    def add_f90_skip(cases):
        # F90 was passed in _mutate's base; flip it to skipped.
        for case in cases:
            if case.get("name", "").startswith("test_F90_substituted"):
                cases.remove(case)
                cases.append(
                    _case(
                        "tests/test_m7_layer6_ingress.py::test_F90_substituted_pathname_bytes_come_from_validated_handle",
                        "skipped",
                        "symlink creation requires privileges on this platform",
                    )
                )
        return cases

    p = _mutate(tmp_path, "f90skip", add_f90_skip)
    assert inv.verify_run(str(p)) == 1


def test_verifier_rejects_wrong_skip_reason(tmp_path: Path) -> None:
    def flip(cases):
        for i, case in enumerate(cases):
            if case.get("name") == "test_T65_endpoint_rendezvous_loss_is_not_ownership_loss":
                cases[i] = _case(
                    "tests/test_m7_layer7_supported_faults.py::test_T65_endpoint_rendezvous_loss_is_not_ownership_loss",
                    "skipped",
                    "some unrelated reason",
                )
        return cases

    p = _mutate(tmp_path, "wrongreason", flip)
    assert inv.verify_run(str(p)) == 1


def test_verifier_rejects_failed_mandatory_test(tmp_path: Path) -> None:
    def fail_one(cases):
        cases[0] = _case(sorted(ALL_NODES)[0], "failure", "assertion")
        return cases

    p = _mutate(tmp_path, "failed", fail_one)
    assert inv.verify_run(str(p)) == 1


def test_verifier_rejects_malformed_xml(tmp_path: Path) -> None:
    p = tmp_path / "bad.xml"
    p.write_text("<testsuite><testcase>", encoding="utf-8")
    assert inv.verify_run(str(p)) == 1
