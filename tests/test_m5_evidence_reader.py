"""Tests for the Layer-5 lease-coordinated evidence reader."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader


class _SB:
    pass


def _brokers(tmp_path: Path) -> tuple[M5LeasedWriteBroker, M5LeasedWriteBroker]:
    sb = _SB()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    first = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    second = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    return first, second


async def test_reader_exposes_only_readback_surface(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    first, _ = _brokers(tmp_path)
    calls: list[str] = []

    async def read_bookmark(
        broker: M5WriteBroker,
        post_url: str,
    ) -> ActionResult:
        calls.append(post_url)
        return ok_result(data={"bookmark_state": "bookmarked"})

    monkeypatch.setattr(M5WriteBroker, "read_bookmark_state", read_bookmark)
    reader = M5LeasedEvidenceReader(first)

    result = await reader.read_bookmark_state("https://x.com/i/status/123")

    assert result.ok is True
    assert calls == ["https://x.com/i/status/123"]
    assert not hasattr(reader, "click_bookmark")
    assert not hasattr(reader, "click_like")
    assert not hasattr(reader, "click_submit")
    assert not hasattr(reader, "delete_post")


async def test_reader_refuses_navigation_while_content_context_is_owned(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    first, second = _brokers(tmp_path)
    calls: list[str] = []

    async def read_like(
        broker: M5WriteBroker,
        post_url: str,
    ) -> ActionResult:
        calls.append(post_url)
        return ok_result(data={"like_state": "liked"})

    monkeypatch.setattr(M5WriteBroker, "read_like_state", read_like)
    first._m5_write_state.content_owner = first._m5_lease_owner
    reader = M5LeasedEvidenceReader(second)

    result = await reader.read_like_state("https://x.com/i/status/123")

    assert result.ok is False
    assert calls == []


async def test_reader_uses_shared_browser_lock_state_across_brokers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    first, second = _brokers(tmp_path)
    assert first._m5_write_state is second._m5_write_state

    calls: list[str] = []

    async def read_like(
        broker: M5WriteBroker,
        post_url: str,
    ) -> ActionResult:
        calls.append(post_url)
        return ok_result(data={"like_state": "liked"})

    monkeypatch.setattr(M5WriteBroker, "read_like_state", read_like)
    reader = M5LeasedEvidenceReader(second)

    result = await reader.read_like_state("https://x.com/i/status/123")

    assert result.ok is True
    assert calls == ["https://x.com/i/status/123"]
