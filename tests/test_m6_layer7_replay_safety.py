"""M6 Layer-7 evidence-driven like/unlike replay-safety qualification.

These tests qualify the concrete supported live broker mechanics required by
M6_DESIGN.md §18.  They intentionally do not promote policy: DOM convergence
cannot establish absence of residual server/platform engagement effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m6_replay_qualified_write_broker import M6ReplayQualifiedWriteBroker
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    ReplaySemantics,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_authority_factory import build_live_m5_write_broker

URL = "https://x.com/actor/status/123"


class _LikePage:
    def __init__(self, state: str = "not_liked") -> None:
        self.state = state
        self.state_sequence: list[str] = []
        self.events: list[str] = []

    def next_probe_state(self) -> str:
        if self.state_sequence:
            self.state = self.state_sequence.pop(0)
        return self.state


class _CDP:
    def __init__(self, page: _LikePage) -> None:
        self.page = page

    async def evaluate(self, expr: str) -> ActionResult:
        if "m6-layer7-like-state:123" in expr:
            state = self.page.next_probe_state()
            self.page.events.append(f"probe:{state}")
            value = state if state in {"liked", "not_liked"} else "unknown"
            return ok_result(data={"result": {"value": value}})

        if "m6-layer7-like-click-like:123" in expr:
            self.page.events.append(f"click_attempt:like:{self.page.state}")
            if self.page.state == "not_liked":
                self.page.state = "liked"
                self.page.events.append("click:like")
                value = "clicked"
            elif self.page.state == "both":
                value = "ambiguous"
            else:
                value = "stale"
            return ok_result(data={"result": {"value": value}})

        if "m6-layer7-like-click-unlike:123" in expr:
            self.page.events.append(f"click_attempt:unlike:{self.page.state}")
            if self.page.state == "liked":
                self.page.state = "not_liked"
                self.page.events.append("click:unlike")
                value = "clicked"
            elif self.page.state == "both":
                value = "ambiguous"
            else:
                value = "stale"
            return ok_result(data={"result": {"value": value}})

        raise AssertionError(f"unexpected CDP expression: {expr[:120]!r}")


class _Controller:
    def __init__(self, page: _LikePage) -> None:
        self._cdp = _CDP(page)


class _SB:
    def __init__(self, page: _LikePage) -> None:
        self.page = page
        self._controller = _Controller(page)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.page.events.append(f"navigate:{url}")
        return ok_result(data={"url": url})


def _broker(tmp_path: Path, page: _LikePage) -> M6ReplayQualifiedWriteBroker:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M6ReplayQualifiedWriteBroker(_SB(page), KillSwitch(cfg))  # type: ignore[arg-type]
    broker._LIKE_SETTLE_SECONDS = 0.0
    broker._LIKE_HYDRATION_INTERVAL_SECONDS = 0.0
    return broker


@pytest.mark.parametrize(
    ("initial", "method", "expected", "clicked"),
    [
        ("not_liked", "click_like", "liked", "click:like"),
        ("liked", "click_unlike", "not_liked", "click:unlike"),
    ],
)
async def test_directional_mutation_reaches_exact_requested_state(
    tmp_path: Path,
    initial: str,
    method: str,
    expected: str,
    clicked: str,
) -> None:
    page = _LikePage(initial)
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await getattr(broker, method)(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert result.ok
    assert page.state == expected
    assert gates == ["gate"]
    assert page.events.count(clicked) == 1


@pytest.mark.parametrize(
    ("initial", "method"),
    [
        ("liked", "click_like"),
        ("not_liked", "click_unlike"),
    ],
)
async def test_already_satisfied_direction_is_zero_gate_zero_mutation(
    tmp_path: Path,
    initial: str,
    method: str,
) -> None:
    page = _LikePage(initial)
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await getattr(broker, method)(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert result.ok
    assert page.state == initial
    assert gates == []
    assert not any(event.startswith("click_attempt:") for event in page.events)


@pytest.mark.parametrize("state", ["both", "neither"])
@pytest.mark.parametrize("method", ["click_like", "click_unlike"])
async def test_ambiguous_or_missing_directional_controls_fail_before_commit(
    tmp_path: Path,
    state: str,
    method: str,
) -> None:
    page = _LikePage(state)
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await getattr(broker, method)(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert not result.ok
    assert page.state == state
    assert gates == []
    assert len([event for event in page.events if event.startswith("probe:")]) == 4
    assert not any(event.startswith("click_attempt:") for event in page.events)


async def test_hydration_transition_is_bounded_and_uses_conclusive_direction(
    tmp_path: Path,
) -> None:
    page = _LikePage("neither")
    page.state_sequence = ["neither", "both", "not_liked"]
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await broker.click_like(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert result.ok
    assert page.state == "liked"
    assert gates == ["gate"]
    assert page.events[:4] == [
        f"navigate:{URL}",
        "probe:neither",
        "probe:both",
        "probe:not_liked",
    ]
    assert page.events[-2:] == ["click_attempt:like:not_liked", "click:like"]


@pytest.mark.parametrize(
    ("method", "initial", "changed"),
    [
        ("click_like", "not_liked", "liked"),
        ("click_unlike", "liked", "not_liked"),
        ("click_like", "not_liked", "both"),
        ("click_unlike", "liked", "both"),
    ],
)
async def test_state_change_after_gate_never_falls_through_to_wrong_direction(
    tmp_path: Path,
    method: str,
    initial: str,
    changed: str,
) -> None:
    page = _LikePage(initial)
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    def gate() -> Optional[ActionResult]:
        gates.append("gate")
        page.state = changed
        return None

    result = await getattr(broker, method)(URL, _commit_gate=gate)

    assert not result.ok
    assert gates == ["gate"]
    assert page.state == changed
    assert not any(event in {"click:like", "click:unlike"} for event in page.events)
    assert any(event.startswith("click_attempt:") for event in page.events)


async def test_active_content_owner_blocks_engagement_before_navigation(tmp_path: Path) -> None:
    page = _LikePage("not_liked")
    broker = _broker(tmp_path, page)
    broker._m5_write_state.content_owner = "different-content-owner"

    result = await broker.click_like(URL, _commit_gate=lambda: None)

    assert not result.ok
    assert page.events == []


def test_supported_live_factory_uses_layer7_qualified_broker(tmp_path: Path) -> None:
    page = _LikePage()
    raw = _SB(page)
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))

    broker = build_live_m5_write_broker(raw, kill)

    assert isinstance(broker, M6ReplayQualifiedWriteBroker)


def test_layer7_does_not_promote_like_or_unlike_without_external_replay_evidence() -> None:
    for action in ("like", "unlike"):
        policy = DEFAULT_EFFECT_POLICIES.require(action)
        assert policy.replay_semantics is ReplaySemantics.UNKNOWN
        assert policy.durability is DurabilityPolicy.REQUIRED
