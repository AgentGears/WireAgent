"""delete_post tests (2026-09-23) — the compensation made real.

Dispatcher-level, on the bookmark-dispatch pattern: stubbed session, fake
write broker recording the delete flow, two-phase confirmation through the
real kernel. Failure paths: target not found, menu unavailable, verification
honesty (unknown state is a verify failure, not a pass).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result, soft_failure
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


class _FakeDeleteBroker:
    """Records delete calls; injectable failure point and post-state."""

    def __init__(self, fail_at: Optional[str] = None, state_after: str = "deleted") -> None:
        self.fail_at = fail_at
        self.state_after = state_after
        self.delete_calls: list[tuple[str, str]] = []
        self.state_reads: list[str] = []

    async def delete_post(self, post_url: str, post_id: str) -> Any:
        self.delete_calls.append((post_url, post_id))
        if self.fail_at == "not_found":
            return soft_failure("delete_post: target post not found")
        if self.fail_at == "menu":
            return soft_failure("delete_post: Delete item not in menu")
        if self.fail_at == "confirm":
            return soft_failure("delete_post: confirmation button never appeared")
        return ok_result(data={"deleted": True, "post_id": post_id})

    async def read_post_state(self, post_url: str, post_id: str) -> Any:
        self.state_reads.append(post_id)
        return ok_result(data={"post_state": self.state_after})


def _make_dispatcher(tmp_path: Path, fake_broker) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    d._write_kernel._write_broker_factory = lambda: fake_broker  # type: ignore[attr-defined]
    return d


TARGET = {"post_url": "https://x.com/infaag/status/2102520857155522777"}


async def test_delete_full_pipeline_executes_and_verifies(tmp_path: Path) -> None:
    fake = _FakeDeleteBroker()
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("delete_post", TARGET)
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    token = r1.data["data"]["confirmation_token"]

    r2 = await d.invoke("delete_post", {**TARGET, "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    assert r2.data["trace"]["stages"][-1] == "verified"
    assert fake.delete_calls == [(TARGET["post_url"], "2102520857155522777")]
    assert fake.state_reads == ["2102520857155522777"]
    data = r2.data["data"]
    assert data["result"] == "post_deleted"
    assert data["supports_compensation"] is False


async def test_delete_target_not_found_fails_cleanly(tmp_path: Path) -> None:
    fake = _FakeDeleteBroker(fail_at="not_found")
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("delete_post", TARGET)
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("delete_post", {**TARGET, "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "deny"
    assert r2.data["trace"]["execute_ok"] is False
    # Honesty (2026-09-22 fix): verify is SKIPPED after a failed execute.
    assert "verify_skipped_execute_failed" in r2.data["trace"]["stages"]
    assert fake.state_reads == [], "no state read after failed execute"


async def test_delete_menu_unavailable_fails_cleanly(tmp_path: Path) -> None:
    """Foreign posts show no Delete item — the honest failure path."""
    fake = _FakeDeleteBroker(fail_at="menu")
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("delete_post", TARGET)
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("delete_post", {**TARGET, "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "deny"
    assert r2.data["trace"]["execute_ok"] is False


async def test_delete_verify_unknown_is_not_verified(tmp_path: Path) -> None:
    """Executed, but the post state reads 'unknown' — verify FAILS honestly
    (ok=True means verified); the verdict stays allow (execution happened)."""
    fake = _FakeDeleteBroker(state_after="unknown")
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("delete_post", TARGET)
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("delete_post", {**TARGET, "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    assert r2.data["trace"]["verify_ok"] is False
    assert "verify_failed" in r2.data["trace"]["stages"]


async def test_delete_intent_mismatch_blocked(tmp_path: Path) -> None:
    """Token for post A cannot delete post B."""
    fake = _FakeDeleteBroker()
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("delete_post", TARGET)
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("delete_post", {
        "post_url": "https://x.com/infaag/status/999999",
        "confirmation_token": token,
    })
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"
    assert fake.delete_calls == []


async def test_delete_preview_warns_when_target_absent(tmp_path: Path) -> None:
    """Preview is read-only and never raises; an absent target is a warning,
    not an exception (the stubbed read broker cannot navigate — the preview's
    existence probe degrades to 'unknown' gracefully)."""
    from webwire.capabilities.delete_post import DeletePostCapability

    class _ProbeBroker:
        async def navigate(self, url, **_):
            return ok_result(data={"url": url})

        async def probe_selectors(self, specs):
            return ok_result(data={
                "probes": {name: False for name in specs}, "count": len(specs),
            })

    cap = DeletePostCapability()
    intent = cap.compose(TARGET, actor_identity="t")
    pv = await cap.preview(intent, _ProbeBroker())
    assert pv.current_state == "post state: absent"
    assert any("TARGET NOT FOUND" in w for w in pv.warnings)
    assert any("IRREVERSIBLE" in w for w in pv.warnings)
