"""Kernel-hygiene tests — final P0 batch (2026-09-22).

Three fixes, one per decided spec:
1. Actor identity: whoami binds the resolved handle on the SessionManager;
   the dispatcher passes it into every write intent's dedupe key (was a
   getattr on an attribute nothing ever set — actor stayed "?").
2. Verify honesty: verify runs ONLY after a successful execute (was: ran after
   failures too, wasting browser work and logging verify_ok beside
   execute_ok=False); and an "unknown" state read is a verification FAILURE,
   not a pass (live E2E observed bookmark verify_ok=True on state=unknown).
3. Side-effect-free executes (dry_run=True, compose_post's deliberate no-op)
   record NO dedupe key — a confirmed dry-run no longer blocks the real
   same-text post_text for the dedupe TTL.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from super_browser.results import ActionError, ErrorCategory, action_result

from webwire.capabilities.bookmark import BookmarkCapability
from webwire.capabilities.like import LikeCapability
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    KillSwitch,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult
from webwire.session import SessionManager


def _make_kernel(tmp_path: Path) -> tuple[WriteKernel, DedupeStore]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    dedupe = DedupeStore(ttl_seconds=3600)
    kernel = WriteKernel(KillSwitch(cfg), DEFAULT_REGISTRY, TokenBucket(), dedupe, Journal(cfg))
    return kernel, dedupe


class _RegistryCap:
    """Registry-passing fake write capability with a programmable execute."""

    def __init__(self, action_type: str, execute_result: ActionResult) -> None:
        self._action = action_type
        self._execute_result = execute_result
        self.execute_called = False
        self.verify_called = False
        self.name = action_type

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.get(self._action)
        return WriteIntent(
            action_type=self._action, target_type="post", target_id="1",
            risk_meta=meta, compensation=comp,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        return PreviewResult(summary=f"Will {self._action}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.execute_called = True
        return self._execute_result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.verify_called = True
        return ok_result(data={"verified": True})


# ---------------------------------------------------------------------------
# 1. Actor identity
# ---------------------------------------------------------------------------

async def test_whoami_hook_binds_actor_identity(tmp_path: Path) -> None:
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    who = ok_result(data={
        "handle": "infaag", "display_name": None,
        "profile_url": "https://x.com/infaag",
        "session_status": "authenticated", "source": "profile_link_href",
    })
    await d._post_whoami_hook(who)
    assert d._session.resolved_handle == "infaag"
    assert d._session.authenticated is True


async def test_whoami_hook_without_handle_keeps_existing_identity(tmp_path: Path) -> None:
    """A handle-less whoami result must not clear a known-good identity."""
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    d._session.set_resolved_handle("infaag")
    await d._post_whoami_hook(ok_result(data={"session_status": "degraded"}))
    assert d._session.resolved_handle == "infaag"


def test_set_resolved_handle_ignores_empty(tmp_path: Path) -> None:
    sm = SessionManager(WebWireConfig(state_dir=tmp_path))
    sm.set_resolved_handle("")
    sm.set_resolved_handle(None)
    sm.set_resolved_handle("   ")
    assert sm.resolved_handle is None
    sm.set_resolved_handle(" infaag ")
    assert sm.resolved_handle == "infaag"


async def test_actor_flows_into_write_dedupe_key(tmp_path: Path) -> None:
    """End-to-end plumbing: after identity is bound, a write invocation's
    dedupe key carries the handle (was '?|bookmark|...')."""
    from webwire.broker import ReadOnlyBroker

    class _StubSB:
        _page = None
        _controller = None

    class _StubSessionManager(SessionManager):
        def __init__(self, config: WebWireConfig) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    d._session.set_resolved_handle("infaag")

    r = await d.invoke("bookmark_post", {"post_url": "https://x.com/a/status/1"})
    assert r.ok is True
    assert r.data["trace"]["intent"]["dedupe_key"].startswith("infaag|")


async def test_invoke_whoami_awaits_the_hook(tmp_path: Path) -> None:
    """Regression (caught live 2026-09-22): the post-whoami hook was called
    WITHOUT await, so it never ran — the live fixture post journaled a '?'
    actor. This drives the REAL invoke() call site with a stubbed whoami
    capability and asserts identity actually binds."""
    from webwire.broker import ReadOnlyBroker
    from webwire.capabilities.base import Capability, CapabilityTier

    class _StubSB:
        _page = None
        _controller = None

    class _StubSessionManager(SessionManager):
        def __init__(self, config: WebWireConfig) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True

    class _FakeWhoamiCap:
        name = "whoami"
        tier = CapabilityTier.READ

        async def run(self, broker, input):
            return ok_result(data={
                "handle": "ghost", "profile_url": "https://x.com/ghost",
                "session_status": "authenticated",
            })

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    d._registry._caps["whoami"] = _FakeWhoamiCap()  # type: ignore[assignment]

    r = await d.invoke("whoami")
    assert r.ok is True
    assert d._session.resolved_handle == "ghost", (
        "the invoke() path must AWAIT the post-whoami hook — a missing await "
        "silently skips identity binding"
    )
    assert d._session.authenticated is True


# ---------------------------------------------------------------------------
# 2. Verify honesty
# ---------------------------------------------------------------------------

def test_verify_skipped_when_execute_fails(tmp_path: Path) -> None:
    failed = action_result(
        ok=False,
        error=ActionError(ErrorCategory.UNKNOWN, "execute failed", recoverable=False),
    )
    cap = _RegistryCap("like", failed)
    kernel, _ = _make_kernel(tmp_path)

    r1 = asyncio.run(kernel.execute(cap, object(), {}))
    token = r1.data["data"]["confirmation_token"]
    r2 = asyncio.run(kernel.execute(cap, object(), {"confirmation_token": token}))

    assert r2.ok is False
    assert r2.data["trace"]["execute_ok"] is False
    assert r2.data["trace"]["verify_ok"] is False
    assert "verify_skipped_execute_failed" in r2.data["trace"]["stages"]
    assert cap.verify_called is False, "verify must not run after a failed execute"


class _StateBroker:
    """Returns a fixed bookmark/like state for verify()."""

    def __init__(self, bookmark_state: str, like_state: str) -> None:
        self._bm = bookmark_state
        self._lk = like_state

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        return ok_result(data={"bookmark_state": self._bm})

    async def read_like_state(self, post_url: str) -> ActionResult:
        return ok_result(data={"like_state": self._lk})


def _intent_for(cap, action: str) -> WriteIntent:
    return cap.compose({"post_url": "https://x.com/a/status/1"}, actor_identity="t")


def test_bookmark_verify_unknown_and_not_bookmarked_are_failures(tmp_path: Path) -> None:
    cap = BookmarkCapability()
    intent = _intent_for(cap, "bookmark")

    unknown = asyncio.run(cap.verify(intent, _StateBroker("unknown", "unknown")))
    assert unknown.ok is False, "state=unknown must not count as verified"

    removed = asyncio.run(cap.verify(intent, _StateBroker("not_bookmarked", "not_liked")))
    assert removed.ok is False, "state=not_bookmarked must not count as verified"

    confirmed = asyncio.run(cap.verify(intent, _StateBroker("bookmarked", "liked")))
    assert confirmed.ok is True


def test_like_verify_unknown_and_not_liked_are_failures(tmp_path: Path) -> None:
    cap = LikeCapability()
    intent = _intent_for(cap, "like")

    unknown = asyncio.run(cap.verify(intent, _StateBroker("bookmarked", "unknown")))
    assert unknown.ok is False, "state=unknown must not count as verified"

    unliked = asyncio.run(cap.verify(intent, _StateBroker("bookmarked", "not_liked")))
    assert unliked.ok is False, "state=not_liked must not count as verified"

    confirmed = asyncio.run(cap.verify(intent, _StateBroker("bookmarked", "liked")))
    assert confirmed.ok is True


# ---------------------------------------------------------------------------
# 3. Side-effect-free executes record no dedupe key
# ---------------------------------------------------------------------------

def test_dry_run_execute_records_no_dedupe(tmp_path: Path) -> None:
    """compose_post's shape: execute returns ok with dry_run=True (deliberate
    no-op). It must NOT record a dedupe key — a confirmed dry-run must not
    block the real same-text post for the TTL."""
    no_op = ok_result(data={"dry_run": True, "note": "no submit path"})
    cap = _RegistryCap("post", no_op)
    kernel, dedupe = _make_kernel(tmp_path)

    r1 = asyncio.run(kernel.execute(cap, object(), {"text": "hello"}))
    token = r1.data["data"]["confirmation_token"]
    r2 = asyncio.run(kernel.execute(
        cap, object(), {"text": "hello", "confirmation_token": token}
    ))
    assert r2.ok is True
    assert r2.data["trace"]["dedupe_recorded"] is False
    assert dedupe.size() == 0

    # The identical invocation is NOT blocked — it reaches confirmation again.
    r3 = asyncio.run(kernel.execute(cap, object(), {"text": "hello"}))
    assert r3.ok is True
    assert r3.data["policy"]["verdict"] == "confirmation_required"
