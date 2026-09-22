"""Kernel registry-gate tests — P0 gap-3 fix (2026-09-22).

The kernel's policy stage now runs on REGISTRY truth, not capability
self-declaration:
- An action_type missing from the risk registry is DENIED (unknown_action) —
  previously it sailed through the no_limit path with self-declared meta.
- Declared RiskMeta/CompensationMeta that differs from the registry entry is
  DENIED (risk_meta_mismatch) — catches both drift (capability bug) and
  downgrade attempts (e.g. labeling a public post private_reversible).

The pass-through canary asserts a legitimate capability still clears the gate,
and test_write_config_consistency.py::test_every_write_capability_meta_matches_registry
guarantees no REAL capability trips it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    KillSwitch,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


def _make_kernel(tmp_path: Path) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        KillSwitch(cfg), DEFAULT_REGISTRY, TokenBucket(),
        DedupeStore(ttl_seconds=3600), Journal(cfg),
    )


class _TrackingCap:
    """Minimal write capability that records whether execute() was reached."""

    def __init__(self, action_type: str, meta, comp) -> None:
        self._action = action_type
        self._meta = meta
        self._comp = comp
        self.executed = False
        self.name = action_type

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        return WriteIntent(
            action_type=self._action, target_type="post", target_id="1",
            risk_meta=self._meta, compensation=self._comp,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        return PreviewResult(summary=f"Will {self._action}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.executed = True
        return ok_result(data={self._action: intent.target_id})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        return ok_result(data={"verified": True})


def test_unknown_action_type_is_denied(tmp_path: Path) -> None:
    """The original bypass shape: an action_type in neither the registry nor
    the bucket. Previously: no_limit + self-declared meta. Now: denied before
    any budget is consumed or preview runs."""
    meta, comp = DEFAULT_REGISTRY.get("bookmark")  # borrowed legit-looking meta
    cap = _TrackingCap("flurb", meta, comp)

    r = asyncio.run(_make_kernel(tmp_path).execute(cap, object(), {}))

    assert r.ok is False
    assert r.data["policy"]["blocked_by"] == "unknown_action"
    assert "denied:unknown_action" in r.data["trace"]["stages"]
    assert cap.executed is False


def test_downgraded_risk_meta_is_denied(tmp_path: Path) -> None:
    """A registered action_type (post) whose declared meta claims a lower tier
    than the registry (bookmark's PRIVATE_REVERSIBLE) — the downgrade attempt
    the self-declaration design permitted."""
    downgraded_meta, _ = DEFAULT_REGISTRY.get_meta("bookmark"), None
    _, real_comp = DEFAULT_REGISTRY.get("post")
    cap = _TrackingCap("post", downgraded_meta, real_comp)

    r = asyncio.run(_make_kernel(tmp_path).execute(cap, object(), {}))

    assert r.ok is False
    assert r.data["policy"]["blocked_by"] == "risk_meta_mismatch"
    assert "denied:risk_meta_mismatch" in r.data["trace"]["stages"]
    assert cap.executed is False


def test_drifted_compensation_meta_is_denied(tmp_path: Path) -> None:
    """Meta matches but compensation drifts (claims supports_compensation=False
    where the registry says delete_post exists) — still a mismatch, still denied."""
    real_meta, _ = DEFAULT_REGISTRY.get("post")
    from webwire.safety import CompensationMeta
    drifted_comp = CompensationMeta(supports_compensation=False)
    cap = _TrackingCap("post", real_meta, drifted_comp)

    r = asyncio.run(_make_kernel(tmp_path).execute(cap, object(), {}))

    assert r.ok is False
    assert r.data["policy"]["blocked_by"] == "risk_meta_mismatch"
    assert cap.executed is False


def test_legitimate_capability_passes_the_gate(tmp_path: Path) -> None:
    """Canary: a capability whose compose() embeds the registry entry verbatim
    (what every real capability does) still clears the gate and reaches
    confirmation_required."""
    meta, comp = DEFAULT_REGISTRY.get("like")
    cap = _TrackingCap("like", meta, comp)

    r = asyncio.run(_make_kernel(tmp_path).execute(cap, object(), {}))

    assert r.ok is True
    assert r.data["policy"]["verdict"] == "confirmation_required"
    assert "confirmation_required" in r.data["trace"]["stages"]
    assert "denied:unknown_action" not in r.data["trace"]["stages"]
    assert "denied:risk_meta_mismatch" not in r.data["trace"]["stages"]
    assert cap.executed is False  # execute only after confirmation
