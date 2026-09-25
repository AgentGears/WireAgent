"""Hydration regressions for Layer-5 actor-bound post evidence."""

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


async def test_post_text_waits_for_direct_text_node_hydration(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_actor_bound_evidence.asyncio.sleep", _no_sleep)
    sb = _SB(
        [
            {"status": "pending_text"},
            {
                "status": "found",
                "actor": "Actor",
                "id": "123",
                "path": "/Actor/status/123",
                "text": "approved text",
            },
        ]
    )
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    reader = M5ActorBoundEvidenceReader(broker)

    result = await reader.verify_post_text(
        "https://x.com/Actor/status/123",
        "approved text",
    )

    assert result.ok is True
    assert result.data["post_id"] == "123"
    assert result.data["post_actor"] == "Actor"
    assert len(sb._controller._cdp.expressions) == 2
    expr = sb._controller._cdp.expressions[0]
    assert "status:'pending_text'" in expr
    assert "status:'ambiguous_text'" in expr
