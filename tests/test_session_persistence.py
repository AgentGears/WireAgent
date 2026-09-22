"""Tests for the session-persistence + attach-gate + ownership invariants
added in the cookie-persistence review iteration.

Covers:
- attach mode refused when allow_attach=False (review Q4 invariant)
- ownership mode reported correctly (owned | attached)
- checkpoint_session refused when not authenticated (review Q3: never save
  a logged-out jar over a known-good file)
- mark_authenticated enables checkpointing
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ok_result
from webwire.session import SessionManager


class _StubSB:
    """Stub facade with async save_session/load_session/stop."""
    def __init__(self) -> None:
        self.saved_to: list[str] = []
        self.loaded_from: list[str] = []
    async def save_session(self, path: str) -> Any:
        self.saved_to.append(path)
        Path(path).write_text('{"version":"1.0","cookies":[]}')  # noqa: ASYNC240 — test fake; the blocking write is the point
        return ok_result(data={"path": path})
    async def load_session(self, path: str) -> Any:
        self.loaded_from.append(path)
        return ok_result(data={"path": path, "cookie_count": 3})
    async def stop(self) -> None:
        pass


def _make_mgr(tmp_path: Path, **overrides: Any) -> SessionManager:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None, **overrides)
    mgr = SessionManager(cfg)
    return mgr


async def test_attach_refused_without_allow_attach(tmp_path: Path) -> None:
    """review Q4 invariant: attach mode refused by default."""
    mgr = _make_mgr(tmp_path, session_mode_override="patchright_attach")
    # allow_attach defaults to False
    r = await mgr.start()
    assert r.ok is False
    assert r.failure_category == FailureCategory.SECURITY
    assert "allow_attach" in (r.error.message if r.error else "")


def test_ownership_owned_for_launch(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path)  # default = launch
    assert mgr.ownership == "owned"


def test_ownership_attached_when_configured(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, session_mode_override="patchright_attach")
    assert mgr.ownership == "attached"


async def test_checkpoint_refused_when_not_authenticated(tmp_path: Path) -> None:
    """review Q3 invariant: never save a logged-out jar over a known-good file."""
    mgr = _make_mgr(tmp_path)
    mgr._sb = _StubSB()  # type: ignore[assignment]
    mgr._started = True
    mgr._authenticated = False
    r = await mgr.checkpoint_session()
    assert r.ok is True  # not an error, just skipped
    assert r.data == {"skipped": "not_authenticated"}
    # Underlying save_session must NOT have been called.
    assert mgr._sb.saved_to == []  # type: ignore[attr-defined]


async def test_checkpoint_runs_when_authenticated(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path)
    sb = _StubSB()
    mgr._sb = sb  # type: ignore[assignment]
    mgr._started = True
    mgr._authenticated = True
    r = await mgr.checkpoint_session()
    assert r.ok is True
    assert r.data == {"checkpointed": str(mgr._ww_config.session_path())}
    # save_session was called via a temp file then atomic-replaced.
    assert len(sb.saved_to) == 1
    # Final file exists at the real path.
    assert mgr._ww_config.session_path().exists()


async def test_checkpoint_atomic_writes_to_temp_then_replaces(tmp_path: Path) -> None:
    """The checkpoint must write to a .tmp file then os.replace onto the target,
    so a crash mid-save never leaves a truncated session file."""
    mgr = _make_mgr(tmp_path)
    sb = _StubSB()
    mgr._sb = sb  # type: ignore[assignment]
    mgr._started = True
    mgr._authenticated = True
    await mgr.checkpoint_session()
    # save_session was called with a .tmp path, not the final path directly.
    assert sb.saved_to[0].endswith(".json.tmp")
    # And the final path now exists (the replace happened).
    assert mgr._ww_config.session_path().exists()
    # No leftover temp file.
    assert not mgr._ww_config.session_path().with_suffix(".json.tmp").exists()


async def test_stop_does_not_save_when_unauthenticated(tmp_path: Path) -> None:
    """Clean stop must NOT checkpoint if never authenticated."""
    mgr = _make_mgr(tmp_path)
    sb = _StubSB()
    mgr._sb = sb  # type: ignore[assignment]
    mgr._started = True
    mgr._authenticated = False
    await mgr.stop()
    assert sb.saved_to == []


async def test_stop_saves_when_authenticated(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path)
    sb = _StubSB()
    mgr._sb = sb  # type: ignore[assignment]
    mgr._started = True
    mgr._authenticated = True
    await mgr.stop()
    assert len(sb.saved_to) == 1
    assert not mgr._authenticated  # reset on stop
