"""Evidence-level regressions for M5 reply confirmation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader


class _CDP:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.expressions: list[str] = []

    async def evaluate(self, expr: str) -> ActionResult:
        self.expressions.append(expr)
        payload = self.payloads.pop(0) if self.payloads else {
            "status": "missing_or_ambiguous",
            "targets": 0,
            "replies": 0,
        }
        return ok_result(data={"result": {"value": json.dumps(payload)}})


class _Controller:
    def __init__(self, cdp: _CDP) -> None:
        self._cdp = cdp


class _SB:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._controller = _Controller(_CDP(payloads))
        self.navigations: list[str] = []

    async def navigate(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
    ) -> ActionResult:
        self.navigations.append(url)
        return ok_result(data={"url": url, "wait_until": wait_until})


def _reader(
    tmp_path: Path,
    payloads: list[dict[str, Any]],
) -> tuple[M5LeasedEvidenceReader, _SB]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sb = _SB(payloads)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    return M5LeasedEvidenceReader(broker), sb


def _found(
    *,
    actor: str = "actor",
    text: str = "approved reply",
    target_index: int = 0,
    reply_index: int = 1,
) -> dict[str, Any]:
    return {
        "status": "found",
        "targetIndex": target_index,
        "replyIndex": reply_index,
        "replyActor": actor,
        "replyPath": "/actor/status/222",
        "replyText": text,
    }


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


async def test_reply_evidence_requires_exact_thread_actor_and_text(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [_found()])

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
        "reply_actor": "actor",
        "reply_url": "https://x.com/actor/status/222",
        "text_matches": True,
    }
    assert sb.navigations == ["https://x.com/i/status/111"]


def test_reply_evidence_js_ignores_nested_quote_ownership(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [_found()])

    import asyncio

    asyncio.run(
        reader.verify_reply_in_thread(
            target_post_id="111",
            reply_post_id="222",
            actor_id="@actor",
            normalized_text="approved reply",
        )
    )

    expr = sb._controller._cdp.expressions[0]
    assert "a.closest('article')!==art" in expr
    assert "ts[i].closest('article')===art" in expr
    assert "targets.length!==1||replies.length!==1" in expr


async def test_reply_evidence_rejects_wrong_actor(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(actor="other")])

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@actor",
        normalized_text="approved reply",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "actor" in result.error.message


async def test_reply_evidence_rejects_wrong_text(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(text="different")])

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@actor",
        normalized_text="approved reply",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "text" in result.error.message


async def test_reply_evidence_rejects_reply_before_target(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(target_index=4, reply_index=2)])

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@actor",
        normalized_text="approved reply",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "downstream" in result.error.message


async def test_reply_evidence_missing_or_ambiguous_never_confirms(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_evidence_reader.asyncio.sleep", _no_sleep)
    missing = {
        "status": "missing_or_ambiguous",
        "targets": 1,
        "replies": 0,
    }
    reader, sb = _reader(tmp_path, [missing] * 20)

    result = await reader.verify_reply_in_thread(
        target_post_id="111",
        reply_post_id="222",
        actor_id="@actor",
        normalized_text="approved reply",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert len(sb._controller._cdp.expressions) == 20
