"""Health capability tests (P1 refresh, 2026-09-22).

The 2026-09-22 live E2E caught health reporting ready=true while 3/5
selector probes failed (probes inferred from the observe() snapshot, whose
targets stopped carrying roles). The refresh makes probes DOM-direct
(broker.probe_selectors, live-verified selector set with alternatives) and
gates `ready` on the three CORE probes.

Cases:
a. all probes pass (DOM-direct)      -> ready, ok, basis=dom_probes
b. core probe fails (DOM says no)    -> NOT ready, surfaced
c. supplementary probe fails         -> ready (core pass), readiness.ok=False
d. DOM probe round-trip fails        -> observation fallback still works
e. login wall                        -> auth_required envelope
"""

from __future__ import annotations

from typing import Any

from webwire.capabilities.health import _CORE_PROBES, HealthCapability
from webwire.config import WebWireConfig
from webwire.envelope import ok_result, soft_failure
from webwire.safety import KillSwitch
from webwire.session import SessionManager


class _FakeBroker:
    """Programmable read-only broker for health."""

    def __init__(
        self,
        dom_probes: dict[str, bool] | None = None,
        probe_selectors_ok: bool = True,
        url: str = "https://x.com/home",
    ) -> None:
        self._dom = dom_probes if dom_probes is not None else {
            "main_landmark_or_content": True,
            "navigation_affordance": True,
            "account_affordance": True,
        }
        self._probe_ok = probe_selectors_ok
        self._url = url
        self.allowed_methods = frozenset({"navigate", "observe", "probe_selectors"})

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> Any:
        return ok_result(data={"url": url})

    async def observe(self) -> Any:
        return ok_result(data={
            "url": self._url, "title": "Home / X",
            "targets": [], "total_elements": 10,
        })

    async def probe_selectors(self, specs: dict[str, list[str]]) -> Any:
        if not self._probe_ok:
            return soft_failure("cdp unavailable (simulated)")
        # Answer per requested spec: overrides from self._dom, absent keys True.
        return ok_result(data={
            "probes": {name: bool(self._dom.get(name, True)) for name in specs},
            "count": len(specs),
        })


def _health(tmp_path):
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return HealthCapability(KillSwitch(cfg), SessionManager(cfg), cfg)


async def test_all_probes_pass_ready(tmp_path) -> None:
    h = _health(tmp_path)
    r = await h.run(_FakeBroker(), {})
    assert r.ok is True
    sr = r.data["checks"]["selector_readiness"]
    assert sr["ok"] is True and sr["passed"] == sr["total"] == 5
    assert sr["basis"] == "dom_probes"
    assert sr["core_passed"] is True
    assert r.data["ready"] is True


async def test_core_probe_failure_not_ready(tmp_path, monkeypatch) -> None:
    """The live-E2E failure shape, inverted: if the DOM the capabilities
    depend on is missing, ready MUST be false — not a silent pass."""
    import webwire.capabilities.health as health_mod
    monkeypatch.setattr(health_mod, "_HYDRATION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(health_mod, "_HYDRATION_POLL_INTERVAL_S", 0.01)

    h = _health(tmp_path)
    broker = _FakeBroker(dom_probes={
        "main_landmark_or_content": False,  # CORE probe fails
        "navigation_affordance": True,
        "account_affordance": True,
    })
    r = await h.run(broker, {})
    assert r.ok is False
    sr = r.data["checks"]["selector_readiness"]
    assert sr["probes"]["main_landmark_or_content"] is False
    assert sr["core_passed"] is False
    assert r.data["ready"] is False


async def test_supplementary_failure_still_ready_but_surfaced(tmp_path, monkeypatch) -> None:
    """Navigation/account affordances don't gate ready (core subset does), but
    the failure is visible: selector_readiness.ok is strict."""
    import webwire.capabilities.health as health_mod
    monkeypatch.setattr(health_mod, "_HYDRATION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(health_mod, "_HYDRATION_POLL_INTERVAL_S", 0.01)

    h = _health(tmp_path)
    broker = _FakeBroker(dom_probes={
        "main_landmark_or_content": True,
        "navigation_affordance": False,  # supplementary
        "account_affordance": True,
    })
    r = await h.run(broker, {})
    assert r.ok is True
    assert r.data["ready"] is True
    sr = r.data["checks"]["selector_readiness"]
    assert sr["ok"] is False, "strict aggregation: any probe failure shows here"
    assert sr["core_passed"] is True


async def test_dom_probe_failure_falls_back_to_observation(tmp_path, monkeypatch) -> None:
    """If the probe_selectors round-trip itself keeps failing, legacy observe()
    heuristics still answer (targets with roles). Poll constants shrunk so the
    deadline passes immediately."""
    import webwire.capabilities.health as health_mod
    monkeypatch.setattr(health_mod, "_HYDRATION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(health_mod, "_HYDRATION_POLL_INTERVAL_S", 0.01)

    class _RichObsBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__(probe_selectors_ok=False)

        async def observe(self) -> Any:
            return ok_result(data={
                "url": "https://x.com/home", "title": "Home / X",
                "targets": [
                    {"role": "main", "name": ""},
                    {"role": "link", "name": "Home"},
                    {"role": "link", "name": "Profile"},
                    {"role": "tab", "name": "For you"},
                    {"role": "link", "name": "Explore"},
                ],
            })

    h = _health(tmp_path)
    r = await h.run(_RichObsBroker(), {})
    assert r.ok is True
    sr = r.data["checks"]["selector_readiness"]
    assert sr["basis"] == "observation_fallback"
    assert sr["core_passed"] is True
    assert r.data["ready"] is True


async def test_hydration_poll_retries_until_main_probe_passes(tmp_path, monkeypatch) -> None:
    """The live-caught root cause: X renders AFTER domcontentloaded, so the
    first probe round-trip sees an empty DOM. Health must poll and recover —
    not report a healthy session as unready."""
    import webwire.capabilities.health as health_mod
    monkeypatch.setattr(health_mod, "_HYDRATION_POLL_INTERVAL_S", 0.01)

    class _HydratingBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__(dom_probes={
                "main_landmark_or_content": False,  # pre-render snapshot
                "navigation_affordance": False,
                "account_affordance": False,
            })
            self.calls = 0

        async def probe_selectors(self, specs: dict[str, list[str]]) -> Any:
            self.calls += 1
            if self.calls >= 2:
                self._dom = {
                    "main_landmark_or_content": True,  # hydrated
                    "navigation_affordance": True,
                    "account_affordance": True,
                }
            return await super().probe_selectors(specs)

    h = _health(tmp_path)
    broker = _HydratingBroker()
    r = await h.run(broker, {})
    assert broker.calls == 2, "poll must early-exit once hydrated, not keep polling"
    assert r.ok is True
    sr = r.data["checks"]["selector_readiness"]
    assert sr["basis"] == "dom_probes"
    assert sr["passed"] == 5
    assert r.data["ready"] is True


async def test_login_wall_returns_auth_required(tmp_path) -> None:
    h = _health(tmp_path)
    broker = _FakeBroker(url="https://x.com/login")
    r = await h.run(broker, {})
    assert r.ok is False
    assert r.failure_category is not None
    assert r.failure_category.value == "auth_required"


def test_core_probe_names() -> None:
    """The gate is explicit and small: surface, login, main content."""
    assert _CORE_PROBES == ("on_x_surface", "login_wall_absent", "main_landmark_or_content")


async def test_capability_selector_probes_surfaced_not_gating(tmp_path) -> None:
    """2026-09-23 supplementary group: capability selectors (tweetText,
    bookmark, composer) surface strictly, but a failure does NOT gate ready —
    shell landmarks can be healthy while capability testids churn."""
    h = _health(tmp_path)
    broker = _FakeBroker(dom_probes={
        # Shell probes all healthy:
        "main_landmark_or_content": True,
        "navigation_affordance": True,
        "account_affordance": True,
        # Capability selectors churned:
        "feed_article_text": False,
        "feed_bookmark_action": False,
        "composer_inline": True,
    })
    r = await h.run(broker, {})
    assert r.ok is True, "ready is NOT gated by capability selectors"
    cs = r.data["checks"]["capability_selectors"]
    assert cs["ok"] is False
    assert cs["passed"] == 1 and cs["total"] == 3
    assert cs["probes"]["feed_article_text"] is False
    assert "does not gate ready" in cs["note"]


async def test_capability_selector_probes_pass_shape(tmp_path) -> None:
    h = _health(tmp_path)
    r = await h.run(_FakeBroker(), {})
    assert r.ok is True
    cs = r.data["checks"]["capability_selectors"]
    assert cs["ok"] is True
    assert cs["passed"] == cs["total"] == 3
    assert set(cs["probes"]) == {
        "feed_article_text", "feed_bookmark_action", "composer_inline",
    }
