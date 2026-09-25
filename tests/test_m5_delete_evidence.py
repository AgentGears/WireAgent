"""Evidence-level regressions for M5 delete confirmation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_delete_evidence import M5LeasedDeleteEvidenceReader


class _CDP:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.expressions: list[str] = []

    async def evaluate(self, expr: str) -> ActionResult:
        self.expressions.append(expr)
        payload = self.payloads.pop(0) if self.payloads else {"status": "pending"}
        return ok_result(data={"result": {"value": json.dumps(payload)}})


class _Controller:
    def __init__(self, cdp: _CDP) -> None:
        self._cdp = cdp


class _SB:
    def __init__(self, payloads: list[dict[str, Any]], *, nav_ok: bool = True) -> None:
        self._controller = _Controller(_CDP(payloads))
        self.nav_ok = nav_ok
        self.navigations: list[str] = []

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.navigations.append(url)
        if not self.nav_ok:
            return soft_failure("nav failed", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"url": url, "wait_until": wait_until})


def _reader(
    tmp_path: Path,
    payloads: list[dict[str, Any]],
    *,
    nav_ok: bool = True,
) -> tuple[M5LeasedDeleteEvidenceReader, _SB]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sb = _SB(payloads, nav_ok=nav_ok)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    return M5LeasedDeleteEvidenceReader(broker), sb


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


async def test_delete_evidence_proves_unique_direct_target_present(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [{"status": "present"}])

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is True
    assert result.data["post_state"] == "present"
    assert result.data["evidence"] == "unique_direct_target_article"
    assert sb.navigations == ["https://x.com/u/status/123"]


async def test_delete_evidence_accepts_canonical_i_status_fallback(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [{"status": "present"}])

    result = await reader.read_delete_state("https://x.com/i/status/123", "123")

    assert result.ok is True
    assert result.data["post_state"] == "present"
    assert sb.navigations == ["https://x.com/i/status/123"]


async def test_delete_evidence_requires_target_bound_explicit_tombstone(
    tmp_path: Path,
) -> None:
    reader, _ = _reader(
        tmp_path,
        [{"status": "deleted", "tombstone": "This post was deleted"}],
    )

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is True
    assert result.data["post_state"] == "deleted"
    assert result.data["evidence"] == "target_permalink_explicit_delete_tombstone"


async def test_delete_evidence_rejects_non_x_origin_before_navigation(tmp_path: Path) -> None:
    reader, sb = _reader(
        tmp_path,
        [{"status": "deleted", "tombstone": "This post was deleted"}],
    )

    result = await reader.read_delete_state("https://evil.example/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert sb.navigations == []
    assert sb._controller._cdp.expressions == []


async def test_delete_evidence_rejects_input_status_id_mismatch_before_navigation(
    tmp_path: Path,
) -> None:
    reader, sb = _reader(tmp_path, [{"status": "deleted"}])

    result = await reader.read_delete_state("https://x.com/u/status/999", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert sb.navigations == []
    assert sb._controller._cdp.expressions == []


async def test_delete_evidence_blank_or_unloaded_page_stays_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.safety.m5_delete_evidence.asyncio.sleep", _no_sleep)
    reader, sb = _reader(tmp_path, [{"status": "pending"}] * 20)

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert len(sb._controller._cdp.expressions) == 20


async def test_delete_evidence_ambiguous_target_stays_unknown(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [{"status": "ambiguous", "matches": 2}])

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_delete_evidence_wrong_permalink_stays_unknown(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [{"status": "wrong_page", "path": "/home"}])

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_delete_evidence_redirect_to_non_x_origin_stays_unknown(tmp_path: Path) -> None:
    reader, _ = _reader(
        tmp_path,
        [{"status": "wrong_page", "host": "evil.example", "path": "/u/status/123"}],
    )

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_delete_evidence_redirect_to_different_status_id_stays_unknown(
    tmp_path: Path,
) -> None:
    reader, _ = _reader(
        tmp_path,
        [{"status": "wrong_page", "host": "x.com", "path": "/u/status/999"}],
    )

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_delete_evidence_ambiguous_tombstones_stay_unknown(tmp_path: Path) -> None:
    reader, _ = _reader(tmp_path, [{"status": "ambiguous_tombstone", "count": 2}])

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"


async def test_delete_evidence_navigation_failure_is_not_deletion(tmp_path: Path) -> None:
    reader, sb = _reader(tmp_path, [], nav_ok=False)

    result = await reader.read_delete_state("https://x.com/u/status/123", "123")

    assert result.ok is False
    assert result.failure_category.value == "unknown"
    assert sb._controller._cdp.expressions == []


def test_delete_evidence_js_binds_origin_permalink_id_and_ignores_generic_errors(
    tmp_path: Path,
) -> None:
    reader, sb = _reader(tmp_path, [{"status": "present"}])

    import asyncio

    asyncio.run(reader.read_delete_state("https://x.com/u/status/123", "123"))

    expr = sb._controller._cdp.expressions[0]
    assert "host!=='x.com'&&host!=='www.x.com'" in expr
    assert "statusMatch=path.match" in expr
    assert "statusMatch[1]!==target" in expr
    assert "if(!statusMatch||statusMatch[1]!==target)" in expr
    assert "a.closest('article')!==art||!a.querySelector('time')" in expr
    assert "n.closest('article')" in expr
    assert "if(!wrapped)stones.push(t)" in expr
    assert "stones.length===1" in expr
    assert "stones.length>1" in expr
    assert "This post was deleted" in expr
    assert "Something went wrong" not in expr
    assert "This post is unavailable" not in expr
