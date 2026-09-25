"""Tests for the Layer-5 lease-coordinated media evidence surface."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_media_evidence import M5LeasedMediaEvidenceReader


class _SB:
    pass


def _brokers(tmp_path: Path) -> tuple[M5LeasedWriteBroker, M5LeasedWriteBroker]:
    sb = _SB()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    first = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    second = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    return first, second


async def test_media_evidence_reports_exact_count_without_mutation_surface(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    first, _ = _brokers(tmp_path)
    calls: list[str] = []

    async def count_media(broker, post_url: str) -> int:  # type: ignore[no-untyped-def]
        calls.append(post_url)
        return 3

    monkeypatch.setattr("webwire.safety.m5_media_evidence.count_post_media", count_media)
    reader = M5LeasedMediaEvidenceReader(first)

    result = await reader.count_post_media("https://x.com/actor/status/999")

    assert result.ok is True
    assert result.data["media_count"] == 3
    assert result.data["source_byte_equivalence_verified"] is False
    assert calls == ["https://x.com/actor/status/999"]
    assert not hasattr(reader, "attach_media")
    assert not hasattr(reader, "click_submit")


async def test_media_evidence_refuses_navigation_while_composer_is_owned(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    first, second = _brokers(tmp_path)
    calls: list[str] = []

    async def count_media(broker, post_url: str) -> int:  # type: ignore[no-untyped-def]
        calls.append(post_url)
        return 1

    monkeypatch.setattr("webwire.safety.m5_media_evidence.count_post_media", count_media)
    first._m5_write_state.content_owner = first._m5_lease_owner
    reader = M5LeasedMediaEvidenceReader(second)

    result = await reader.count_post_media("https://x.com/actor/status/999")

    assert result.ok is False
    assert calls == []


async def test_media_evidence_zero_count_is_unknown(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    first, _ = _brokers(tmp_path)

    async def count_media(broker, post_url: str) -> int:  # type: ignore[no-untyped-def]
        return 0

    monkeypatch.setattr("webwire.safety.m5_media_evidence.count_post_media", count_media)
    reader = M5LeasedMediaEvidenceReader(first)

    result = await reader.count_post_media("https://x.com/actor/status/999")

    assert result.ok is False
    assert result.failure_category.value == "unknown"
