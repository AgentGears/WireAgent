"""Regression tests for the final local kill recheck on M5 engagement effects."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.kill_switch import KillSwitch

URL = "https://x.com/u/status/123"


class _NoopSB:
    pass


class _TripDuringProbeBroker(M5WriteBroker):
    def __init__(
        self,
        tmp_path: Path,
        *,
        bookmark_state: str = "not_bookmarked",
        like_state: str = "not_liked",
    ) -> None:
        cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
        self.kill = KillSwitch(cfg)
        super().__init__(_NoopSB(), self.kill)  # type: ignore[arg-type]
        self.bookmark_state = bookmark_state
        self.like_state = like_state
        self.clicks: list[str] = []

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        self.kill.trip()
        return ok_result(data={"bookmark_state": self.bookmark_state})

    async def read_like_state(self, post_url: str) -> ActionResult:
        self.kill.trip()
        return ok_result(data={"like_state": self.like_state})

    async def _click_target_control(
        self,
        post_url: str,
        *,
        testid: str,
        description: str,
    ) -> ActionResult:
        self.clicks.append(testid)
        return ok_result(data={"clicked": True})


@pytest.mark.parametrize(
    ("method_name", "bookmark_state", "like_state"),
    [
        ("click_bookmark", "not_bookmarked", "not_liked"),
        ("click_remove_bookmark", "bookmarked", "not_liked"),
        ("click_like", "not_bookmarked", "not_liked"),
        ("click_unlike", "not_bookmarked", "liked"),
    ],
)
async def test_kill_trip_during_state_probe_blocks_gate_and_click(
    tmp_path: Path,
    method_name: str,
    bookmark_state: str,
    like_state: str,
) -> None:
    broker = _TripDuringProbeBroker(
        tmp_path / method_name,
        bookmark_state=bookmark_state,
        like_state=like_state,
    )
    gate_calls: list[str] = []

    def gate() -> Optional[ActionResult]:
        gate_calls.append("gate")
        return None

    method = getattr(broker, method_name)
    result = await method(URL, _commit_gate=gate)

    assert not result.ok
    assert broker.kill.tripped()
    assert gate_calls == []
    assert broker.clicks == []
