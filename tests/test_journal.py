"""Tests for the audit-only journal surface."""

from __future__ import annotations

import json
from pathlib import Path

from webwire.config import ScreenshotPolicy, WebWireConfig
from webwire.dispatcher import _redact_input, _redact_target
from webwire.journal import Journal, JournalRecord


def _record(trace_id: str = "abc") -> JournalRecord:
    return JournalRecord(
        timestamp="2026-07-08T00:00:00.000+00:00",
        trace_id=trace_id,
        capability="whoami",
        target="https://x.com/home",
    )


def test_journal_appends_one_line_per_record(tmp_path: Path) -> None:
    journal = Journal(WebWireConfig(state_dir=tmp_path))
    record = _record()

    journal.append(record)
    journal.append(record)

    content = (tmp_path / "journal.ndjson").read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == 2


def test_journal_record_serializes_to_valid_json(tmp_path: Path) -> None:
    journal = Journal(WebWireConfig(state_dir=tmp_path))
    record = JournalRecord(
        timestamp="2026-07-08T00:00:00.000+00:00",
        trace_id="t1",
        capability="health",
        result_ok=True,
        success_category="inspection",
        browser_actions=[{"action": "navigate", "duration_ms": 12.0}],
    )

    journal.append(record)

    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["capability"] == "health"
    assert parsed["result_ok"] is True
    assert parsed["browser_actions"][0]["action"] == "navigate"


def test_journal_rotation_preserves_old_and_new_records(tmp_path: Path) -> None:
    """Layer 7 retires hydration, not audit retention/rotation behavior."""

    journal = Journal(
        WebWireConfig(state_dir=tmp_path),
        rotate_max_bytes=1,
        rotate_age_days=31.0,
        retain_rotated=6,
    )

    journal.append(_record("before-rotation"))
    journal.append(_record("after-rotation"))

    rotated = journal.rotated_paths()
    assert len(rotated) == 1
    old_rows = [
        json.loads(line)
        for line in rotated[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    active_rows = [
        json.loads(line)
        for line in (tmp_path / "journal.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [row["trace_id"] for row in old_rows] == ["before-rotation"]
    assert [row["trace_id"] for row in active_rows] == ["after-rotation"]


def test_dispatcher_audit_redaction_strips_query_and_fragment() -> None:
    raw = {
        "post_url": "https://x.com/user/status/123?token=secret#fragment",
        "text": "hello",
    }

    assert _redact_target(raw) == "https://x.com/user/status/123"
    assert _redact_input(raw) == {
        "post_url": "https://x.com/user/status/123",
        "text": "hello",
    }


def test_screenshot_policy_on_failure_default(tmp_path: Path) -> None:
    journal = Journal(WebWireConfig(state_dir=tmp_path))
    assert journal.should_capture_screenshot(failed=True) is True
    assert journal.should_capture_screenshot(failed=False) is False


def test_screenshot_policy_never(tmp_path: Path) -> None:
    journal = Journal(
        WebWireConfig(state_dir=tmp_path, screenshots=ScreenshotPolicy.NEVER)
    )
    assert journal.should_capture_screenshot(failed=True) is False


def test_screenshot_policy_always(tmp_path: Path) -> None:
    journal = Journal(
        WebWireConfig(state_dir=tmp_path, screenshots=ScreenshotPolicy.ALWAYS)
    )
    assert journal.should_capture_screenshot(failed=False) is True


def test_journal_append_real_io_failure_does_not_raise(tmp_path: Path) -> None:
    """Audit I/O failure must never become mutation/control authority."""

    blocked_state_dir = tmp_path / "blocked-state-dir"
    blocked_state_dir.write_text("not a directory", encoding="utf-8")
    journal = Journal(WebWireConfig(state_dir=blocked_state_dir))

    # _ensure() cannot create a child under a file and append() cannot open the
    # resulting journal path. Both are best-effort audit failures and must stay
    # out of the capability/control path.
    journal.append(_record("io-failure"))

    assert blocked_state_dir.is_file()
