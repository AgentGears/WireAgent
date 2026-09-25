"""Evidence-level regressions for M5 quote confirmation."""

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
            "status": "pending_attachment",
            "quoteActor": "actor",
            "quotePath": "/actor/status/222",
            "quoteText": "approved quote",
            "quoteTargetIds": [],
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
    text: str = "approved quote",
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "found",
        "quoteActor": actor,
        "quotePath": "/actor/status/222",
        "quoteText": text,
        "quoteTargetIds": ["111"] if target_ids is None else target_ids,
        "expectedTarget": "111",
    }


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


async def test_quote_evidence_requires_actor_text_and_exact_target(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [_found()])

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@Actor",
        normalized_text="approved quote",
    )

    assert result.ok is True
    assert result.data == {
        "quote_attachment_verified": True,
        "target_post_id": "111",
        "quote_post_id": "222",
        "quote_actor": "actor",
        "quote_url": "https://x.com/actor/status/222",
        "text_matches": True,
    }
    assert sb.navigations == ["https://x.com/i/status/222"]


def test_quote_evidence_js_scopes_attachment_lineage(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [_found()])

    import asyncio

    asyncio.run(
        reader.verify_quote_attachment(
            quote_post_id="222",
            target_post_id="111",
            actor_id="@actor",
            normalized_text="approved quote",
        )
    )

    expr = sb._controller._cdp.expressions[0]
    assert "a.closest('article')!==art" in expr
    assert "[data-testid='quoteTweet']" in expr
    assert "var nested=art.querySelectorAll('article')" in expr
    assert "quoteTargetIds:targets" in expr


async def test_quote_evidence_rejects_wrong_actor(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(actor="other")])

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@actor",
        normalized_text="approved quote",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "actor" in result.error.message


async def test_quote_evidence_rejects_wrong_text(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(text="different")])

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@actor",
        normalized_text="approved quote",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "text" in result.error.message


async def test_quote_evidence_rejects_wrong_target(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(target_ids=["333"])])

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@actor",
        normalized_text="approved quote",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "target" in result.error.message


async def test_quote_evidence_rejects_ambiguous_targets(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [_found(target_ids=["111", "333"])])

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@actor",
        normalized_text="approved quote",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "uniquely" in result.error.message


async def test_quote_evidence_missing_attachment_never_confirms(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_evidence_reader.asyncio.sleep", _no_sleep)
    pending = {
        "status": "pending_attachment",
        "quoteActor": "actor",
        "quotePath": "/actor/status/222",
        "quoteText": "approved quote",
        "quoteTargetIds": [],
    }
    reader, sb = _reader(tmp_path, [pending] * 20)

    result = await reader.verify_quote_attachment(
        quote_post_id="222",
        target_post_id="111",
        actor_id="@actor",
        normalized_text="approved quote",
    )

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert "explicitly visible" in result.error.message
    assert len(sb._controller._cdp.expressions) == 20
