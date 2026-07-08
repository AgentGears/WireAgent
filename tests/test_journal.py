"""Tests for the journal — append, NDJSON shape, screenshot policy."""

from __future__ import annotations

import json
from pathlib import Path

from webwire.config import ScreenshotPolicy, WebWireConfig
from webwire.journal import Journal, JournalRecord


def test_journal_appends_one_line_per_record(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path))
    rec = JournalRecord(
        timestamp="2026-07-08T00:00:00.000+00:00",
        trace_id="abc",
        capability="whoami",
        target="https://x.com/home",
    )
    j.append(rec)
    j.append(rec)
    content = (tmp_path / "journal.ndjson").read_text(encoding="utf-8")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    assert len(lines) == 2


def test_journal_record_serializes_to_valid_json(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path))
    rec = JournalRecord(
        timestamp="2026-07-08T00:00:00.000+00:00",
        trace_id="t1",
        capability="health",
        result_ok=True,
        success_category="inspection",
        browser_actions=[{"action": "navigate", "duration_ms": 12.0}],
    )
    j.append(rec)
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    parsed = json.loads(line)  # must be valid JSON
    assert parsed["capability"] == "health"
    assert parsed["result_ok"] is True
    assert parsed["browser_actions"][0]["action"] == "navigate"


def test_screenshot_policy_on_failure_default(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path))  # default ON_FAILURE
    assert j.should_capture_screenshot(failed=True) is True
    assert j.should_capture_screenshot(failed=False) is False


def test_screenshot_policy_never(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path, screenshots=ScreenshotPolicy.NEVER))
    assert j.should_capture_screenshot(failed=True) is False


def test_screenshot_policy_always(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path, screenshots=ScreenshotPolicy.ALWAYS))
    assert j.should_capture_screenshot(failed=False) is True


def test_journal_append_failure_does_not_raise(tmp_path: Path) -> None:
    """Journal issues must not block capability execution."""
    j = Journal(WebWireConfig(state_dir=tmp_path / "nonexistent" / "deep"))  # parent missing
    rec = JournalRecord(timestamp="t", trace_id="x", capability="whoami")
    # Should log a warning, not raise. Directory will be created by _ensure.
    j.append(rec)
    # After _ensure, the dir exists and the file was written.
    assert (tmp_path / "nonexistent" / "deep" / "journal.ndjson").exists()
