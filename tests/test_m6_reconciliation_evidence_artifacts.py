"""M6 Layer-1 evidence artifact identity regressions."""

from __future__ import annotations

import pytest

from webwire.safety.reconciliation_ledger import canonical_evidence_hash


def _evidence_with_artifact(artifact: object) -> dict:
    return {
        "basis": "operator-inspection",
        "observed_at": "2026-09-26T00:00:00+00:00",
        "observations": [
            {
                "kind": "external-artifact-observation",
                "artifact": artifact,
            }
        ],
    }


def test_external_artifact_requires_content_identity_not_location_only() -> None:
    evidence = _evidence_with_artifact(
        {"location": "file:///tmp/screenshot.png"}
    )

    with pytest.raises(ValueError, match="artifact.kind"):
        canonical_evidence_hash(evidence)


def test_external_artifact_requires_lowercase_sha256() -> None:
    evidence = _evidence_with_artifact(
        {
            "kind": "screenshot",
            "sha256": "A" * 64,
            "location": "file:///tmp/screenshot.png",
        }
    )

    with pytest.raises(ValueError, match="artifact.sha256"):
        canonical_evidence_hash(evidence)


def test_external_artifact_location_is_optional_diagnostic_provenance() -> None:
    evidence = _evidence_with_artifact(
        {
            "kind": "screenshot",
            "sha256": "a" * 64,
        }
    )

    digest = canonical_evidence_hash(evidence)
    assert len(digest) == 64


def test_nested_external_artifact_is_validated_recursively() -> None:
    evidence = {
        "basis": "operator-inspection",
        "observed_at": "2026-09-26T00:00:00+00:00",
        "observations": [
            {
                "kind": "nested",
                "details": {
                    "artifact": {
                        "kind": "dom-snapshot",
                        "sha256": "b" * 64,
                        "location": "evidence/dom.json",
                    }
                },
            }
        ],
    }

    digest = canonical_evidence_hash(evidence)
    assert len(digest) == 64


def test_artifact_location_if_present_must_be_nonempty_string() -> None:
    evidence = _evidence_with_artifact(
        {
            "kind": "screenshot",
            "sha256": "c" * 64,
            "location": "",
        }
    )

    with pytest.raises(ValueError, match="artifact.location"):
        canonical_evidence_hash(evidence)
