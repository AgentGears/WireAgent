"""Hydration regressions for Layer-5 actor-bound content evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_actor_bound_evidence import M5ActorBoundEvidenceReader


class _CDP:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.expressions: list[str] = []

    async def evaluate(self, expr: str) -> ActionResult:
        self.expressions.append(expr)
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return ok_result(data={"result": {"value": json.dumps(payload)}})


class _Controller:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._cdp = _CDP(payloads)


class _SB:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._controller = _Controller(payloads)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        return ok_result(data={"url": url, "wait_until": wait_until})


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _reader(tmp_path: Path, payloads: list[dict[str, Any]]) -> tuple[M5ActorBoundEvidenceReader, _SB]:
    sb = _SB(payloads)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    return M5ActorBoundEvidenceReader(broker), sb


async def test_post_text_waits_for_direct_text_node_hydration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    reader, sb = _reader(
        tmp_path,
        [
            {"status": "pending_text", "actor": "Actor", "id": "123", "path": "/Actor/status/123"},
            {
                "status": "found",
                "actor": "Actor",
                "id": "123",
                "path": "/Actor/status/123",
                "text": "approved text",
            },
        ],
    )

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/123",
        "approved text",
    )

    assert result.ok is True
    assert result.data["post_id"] == "123"
    assert result.data["post_actor"] == "Actor"
    assert "direct_text_absent_stable" not in result.data
    assert len(sb._controller._cdp.expressions) == 2
    expr = sb._controller._cdp.expressions[0]
    assert "status:'pending_text'" in expr
    assert "status:'ambiguous_text'" in expr


async def test_empty_approved_text_accepts_only_stable_direct_text_absence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    pending = {
        "status": "pending_text",
        "actor": "Actor",
        "id": "456",
        "path": "/Actor/status/456",
    }
    reader, sb = _reader(tmp_path, [pending])

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/456",
        "",
    )

    assert result.ok is True
    assert result.data["text_matches"] is True
    assert result.data["direct_status_owned"] is True
    assert result.data["direct_text_absent_stable"] is True
    assert result.data["post_id"] == "456"
    assert result.data["post_actor"] == "Actor"
    # The absence must be observed repeatedly; one transient missing node is
    # never enough to confirm an empty approved media-only post.
    assert len(sb._controller._cdp.expressions) == 8


async def test_reply_waits_for_direct_text_node_hydration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    reader, sb = _reader(
        tmp_path,
        [
            {
                "status": "pending_text",
                "targetIndex": 0,
                "replyIndex": 1,
                "replyActor": "Actor",
                "replyPath": "/Actor/status/222",
            },
            {
                "status": "found",
                "targetIndex": 0,
                "replyIndex": 1,
                "replyActor": "Actor",
                "replyPath": "/Actor/status/222",
                "replyText": "approved reply",
            },
        ],
    )

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@Actor",
        normalized_text="approved reply",
    )

    assert result.ok is True
    assert result.data == {
        "thread_bound": True,
        "target_post_id": "111",
        "reply_post_id": "222",
        "reply_actor": "Actor",
        "reply_url": "https://x.com/Actor/status/222",
        "text_matches": True,
    }
    assert len(sb._controller._cdp.expressions) == 2


async def test_empty_reply_text_requires_stable_direct_text_absence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    pending = {
        "status": "pending_text",
        "targetIndex": 0,
        "replyIndex": 1,
        "replyActor": "Actor",
        "replyPath": "/Actor/status/222",
    }
    reader, sb = _reader(tmp_path, [pending])

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@Actor",
        normalized_text="",
    )

    assert result.ok is True
    assert result.data["thread_bound"] is True
    assert result.data["text_matches"] is True
    assert len(sb._controller._cdp.expressions) == 8


async def test_quote_waits_for_outer_text_node_hydration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    reader, sb = _reader(
        tmp_path,
        [
            {
                "status": "pending_text",
                "quoteActor": "Actor",
                "quotePath": "/Actor/status/333",
                "quoteTargetIds": ["111"],
            },
            {
                "status": "found",
                "quoteActor": "Actor",
                "quotePath": "/Actor/status/333",
                "quoteText": "approved quote",
                "quoteTargetIds": ["111"],
                "expectedTarget": "111",
            },
        ],
    )

    result = await reader.verify_quote_attachment(
        quote_post_id="333",
        target_post_id="111",
        actor_id="@Actor",
        normalized_text="approved quote",
    )

    assert result.ok is True
    assert result.data == {
        "quote_attachment_verified": True,
        "target_post_id": "111",
        "quote_post_id": "333",
        "quote_actor": "Actor",
        "quote_url": "https://x.com/Actor/status/333",
        "text_matches": True,
    }
    assert len(sb._controller._cdp.expressions) == 2


async def test_empty_quote_text_requires_stable_direct_text_absence(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    pending = {
        "status": "pending_text",
        "quoteActor": "Actor",
        "quotePath": "/Actor/status/333",
        "quoteTargetIds": ["111"],
    }
    reader, sb = _reader(tmp_path, [pending])

    result = await reader.verify_quote_attachment(
        quote_post_id="333",
        target_post_id="111",
        actor_id="@Actor",
        normalized_text="",
    )

    assert result.ok is True
    assert result.data["quote_attachment_verified"] is True
    assert result.data["text_matches"] is True
    assert len(sb._controller._cdp.expressions) == 8
