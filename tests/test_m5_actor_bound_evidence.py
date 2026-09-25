"""Adversarial regressions for actor-bound standalone-post evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_actor_bound_evidence import (
    M5ActorBoundEvidenceReader,
    status_url_identity,
)


class _CDP:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.expressions: list[str] = []

    async def evaluate(self, expr: str) -> ActionResult:
        self.expressions.append(expr)
        if not self.payloads:
            payload: Any = []
        elif len(self.payloads) == 1:
            payload = self.payloads[0]
        else:
            payload = self.payloads.pop(0)
        return ok_result(data={"result": {"value": json.dumps(payload)}})


class _Controller:
    def __init__(self, cdp: _CDP) -> None:
        self._cdp = cdp


class _SB:
    def __init__(self, payloads: list[Any]) -> None:
        self._controller = _Controller(_CDP(payloads))
        self.navigations: list[str] = []

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.navigations.append(url)
        return ok_result(data={"url": url, "wait_until": wait_until})


def _reader(
    tmp_path: Path,
    payloads: list[Any],
) -> tuple[M5ActorBoundEvidenceReader, _SB]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sb = _SB(payloads)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    return M5ActorBoundEvidenceReader(broker), sb


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _status(actor: str, post_id: str) -> dict[str, str]:
    return {
        "actor": actor,
        "id": post_id,
        "path": f"/{actor}/status/{post_id}",
    }


def test_status_url_identity_requires_actor_owned_x_permalink() -> None:
    assert status_url_identity("https://x.com/Actor/status/123") == ("actor", "123")
    assert status_url_identity("https://www.x.com/@Actor/status/123/photo/1") == (
        "actor",
        "123",
    )
    assert status_url_identity("https://x.com/i/status/123") is None
    assert status_url_identity("https://example.com/actor/status/123") is None
    assert status_url_identity("https://x.com/actor/status/not-numeric") is None


async def test_stable_direct_baseline_requires_two_equal_snapshots(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    snapshot = [_status("Actor", "10"), _status("Other", "11")]
    reader, sb = _reader(tmp_path, [snapshot, list(reversed(snapshot))])

    result = await reader.capture_pre_submit_ids()

    assert result.ok is True
    assert result.data["status_ids"] == ["10", "11"]
    assert len(sb._controller._cdp.expressions) == 2


async def test_unstable_direct_baseline_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    snapshots = [[_status("Actor", str(index))] for index in range(1, 21)]
    reader, sb = _reader(tmp_path, snapshots)

    result = await reader.capture_pre_submit_ids()

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert len(sb._controller._cdp.expressions) == 20


async def test_post_submit_capture_requires_one_unique_new_direct_status(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    reader, _ = _reader(
        tmp_path,
        [
            [_status("Actor", "10")],
            [_status("Actor", "10"), _status("Actor", "99")],
        ],
    )

    result = await reader.capture_new_post({"10"})

    assert result.ok is True
    assert result.data == {
        "post_id": "99",
        "post_url": "https://x.com/Actor/status/99",
        "post_actor": "Actor",
        "direct_status_owned": True,
    }


async def test_multiple_new_direct_statuses_are_ambiguous(
    tmp_path: Path,
) -> None:
    reader, _ = _reader(
        tmp_path,
        [[_status("Actor", "10"), _status("Actor", "99"), _status("Actor", "100")]],
    )

    result = await reader.capture_new_post({"10"})

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "multiple new direct statuses" in result.error.message


async def test_direct_status_actor_and_text_are_returned_as_evidence(tmp_path: Path) -> None:
    reader, sb = _reader(
        tmp_path,
        [
            {
                "status": "found",
                "actor": "Actor",
                "id": "123",
                "path": "/Actor/status/123",
                "text": "approved text",
            }
        ],
    )

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/123",
        "approved text",
    )

    assert result.ok is True
    assert result.data == {
        "text_matches": True,
        "direct_status_owned": True,
        "post_id": "123",
        "post_actor": "Actor",
        "post_url": "https://x.com/Actor/status/123",
    }
    assert sb.navigations == ["https://x.com/Actor/status/123"]


async def test_same_text_wrong_actor_url_is_not_verified(tmp_path: Path) -> None:
    reader, _ = _reader(
        tmp_path,
        [
            {
                "status": "found",
                "actor": "Other",
                "id": "123",
                "path": "/Other/status/123",
                "text": "approved text",
            }
        ],
    )

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/123",
        "approved text",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_missing_or_ambiguous_direct_owner_stays_unknown(tmp_path: Path) -> None:
    reader, _ = _reader(
        tmp_path,
        [{"status": "missing_or_ambiguous", "matches": 2}],
    )

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/123",
        "approved text",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"


def test_evidence_js_ignores_nested_status_and_text_decoys(tmp_path: Path) -> None:
    reader, sb = _reader(
        tmp_path,
        [
            {
                "status": "found",
                "actor": "Actor",
                "id": "123",
                "path": "/Actor/status/123",
                "text": "approved text",
            }
        ],
    )

    import asyncio

    asyncio.run(reader.verify_post_text("https://x.com/Actor/status/123", "approved text"))

    expr = sb._controller._cdp.expressions[0]
    assert "a.closest('article')!==art||!a.querySelector('time')" in expr
    assert "nodes[i].closest('article')===art" in expr
    assert "matches.length!==1" in expr
