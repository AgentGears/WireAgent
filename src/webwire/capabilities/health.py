"""health capability — probe-set diagnostic with a CORE-probe gate.

Per the Phase 0a design (Point 5 decision), refreshed 2026-09-22 (P1):
- Checks: browser session alive, X reachable, login state known, whoami can
  resolve identity OR returns AUTH_REQUIRED, kill-switch state, read-only broker
  policy active, selector/readiness probes.
- Selector readiness probes the DOM DIRECTLY (broker.probe_selectors —
  selector groups with alternatives, live-verified 2026-09-22) and POLLS
  until hydration: X renders client-side after domcontentloaded, and probing
  before render returns false for everything. The 2026-09-22 live E2E caught
  the prior design failing exactly this way — 3/5 probes failed on a healthy
  logged-in session (probes ran pre-hydration, inferred from an equally
  pre-hydration observe() snapshot), and ready stayed true.
- ``ready`` now REQUIRES the three core probes (on_x_surface,
  login_wall_absent, main_landmark_or_content): if the DOM the capabilities
  depend on isn't there, the diagnostic must say so. Supplementary probes
  (navigation, account affordances) are surfaced but don't gate ready.
- Returns a structured diagnostic blob; ``ok`` reflects overall readiness and
  individual probe failures are surfaced, not hidden.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.broker import ReadOnlyBroker
from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, hard_failure, ok_result, soft_failure
from webwire.safety import KillSwitch
from webwire.session import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["HealthCapability"]

# DOM probe set — live-verified against x.com/home on 2026-09-22 (see
# scripts/diag_selector_battery_live.py). Each probe passes if ANY alternative
# matches. Alternatives hedge single-testid churn.
_PROBE_SELECTOR_SET: dict[str, list[str]] = {
    "main_landmark_or_content": [
        "[data-testid='primaryColumn']",
        "main[role='main']",
        "article",
    ],
    "navigation_affordance": [
        "nav[role='navigation']",
        "[data-testid='AppTabBar_Explore_Link']",
        "a[aria-label][role='link']",
    ],
    "account_affordance": [
        "[data-testid='SideNav_AccountSwitcher_Button']",
        "a[data-testid='AppTabBar_Profile_Link']",
    ],
}

# Probes that gate `ready`. If these fail, the DOM the capabilities depend on
# is not present — the system is NOT ready, whatever navigation reports.
_CORE_PROBES = ("on_x_surface", "login_wall_absent", "main_landmark_or_content")

# Capability-selector probes (2026-09-23) — the selectors the READ/WRITE
# capabilities themselves depend on, probed in the same round-trip as the
# shell set but SUPPLEMENTARY: surfaced strictly, never gating `ready`
# (capability selectors can churn without the shell landmarks moving, and
# vice versa — the re-evaluation's top residual risk). Battery-verified on
# the live home DOM 2026-09-23 (scripts battery via json.dumps embedding):
# tweetText/bookmark present per feed article; composer entries present once.
# Excluded, deliberately: `tweetButton` (modal-only — exists only while a
# compose modal is open; statically 0 on home) and `tweetPhoto`
# (content-dependent — present only when the feed happens to contain photo
# posts; per-post verification is the capabilities' own job).
_CAPABILITY_SELECTOR_SET: dict[str, list[str]] = {
    "feed_article_text": [
        "[data-testid='tweetText']",
    ],
    "feed_bookmark_action": [
        "[data-testid='bookmark']",
        "[data-testid='removeBookmark']",
    ],
    "composer_inline": [
        "[data-testid='tweetButtonInline']",
        "a[data-testid='SideNav_NewTweet_Button']",
    ],
}


class HealthCapability:
    """Aggregate diagnostic across session, identity, kill switch, and probes."""

    name = "health"
    tier = CapabilityTier.READ

    def __init__(
        self,
        kill_switch: KillSwitch,
        session_manager: SessionManager,
        config: Optional[WebWireConfig] = None,
    ) -> None:
        self._kill = kill_switch
        self._session = session_manager
        self._config = config or WebWireConfig()

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        diag: dict[str, Any] = {
            "checks": {},
            "ready": False,
        }

        # 1. Kill switch state (always available).
        diag["checks"]["kill_switch"] = self._kill.state()

        # 2. Browser ownership + session-restore state (review Q4 invariant).
        diag["checks"]["browser"] = {
            "ownership": self._session.ownership,  # owned | attached
            "session": self._session.session_loaded_state,  # no_file|loaded|load_failed|pending
            "authenticated": self._session.authenticated,
        }

        # 3. Read-only broker policy active (structural — always true by construction).
        diag["checks"]["broker_policy"] = {
            "active": True,
            "allowed_methods": sorted(broker.allowed_methods),
        }

        # 3. Navigate home + observe (probes X reachability + DOM readiness together).
        nav = await broker.navigate(self._config.home_url)
        nav_ok = bool(nav.ok)
        diag["checks"]["x_reachable"] = {
            "ok": nav_ok,
            "error": None if nav_ok else (nav.error.message if nav.error else "navigation failed"),
        }

        obs_data: dict[str, Any] = {}
        if nav_ok:
            obs = await broker.observe()
            obs_ok = bool(obs.ok)
            obs_data = (obs.data or {}) if obs_ok else {}
            diag["checks"]["observe"] = {
                "ok": obs_ok,
                "error": None if obs_ok else (obs.error.message if obs.error else "observe failed"),
            }
        else:
            diag["checks"]["observe"] = {"ok": False, "error": "skipped — navigation failed"}

        # 4. Readiness probes — DOM-direct via broker.probe_selectors, POLLED
        # until hydration or deadline. The 2026-09-22 live E2E caught the prior
        # design failing two ways at once: probes ran immediately after
        # domcontentloaded — BEFORE X's React app renders, so every selector
        # probe missed — and they inferred from the observe() snapshot, whose
        # targets were equally pre-hydration. A live selector battery
        # (scripts/diag_selector_battery_live.py) proved all selectors exist
        # on the hydrated DOM; so health now waits for hydration, polling
        # (early-exit as soon as the main-content probe passes) rather than
        # sleeping a fixed time. Observation heuristics remain as fallback
        # only if the DOM probe round-trip itself fails.
        dom_probes: dict[str, bool] = {}
        capability_probes: dict[str, bool] = {}
        if nav_ok:
            dom_probes, capability_probes = await _poll_dom_probes(broker)
        if obs_data:
            diag["checks"]["selector_readiness"] = _probe_readiness(obs_data, dom_probes)
        else:
            diag["checks"]["selector_readiness"] = {
                "ok": False, "probes": {}, "note": "no observation",
                "core_passed": False,
            }
        # Capability-selector probes (2026-09-23): the selectors the read/write
        # capabilities depend on, surfaced SUPPLEMENTARY — strict aggregation,
        # never gating `ready` (they can churn independently of the shell
        # landmarks; the re-evaluation's top residual risk).
        diag["checks"]["capability_selectors"] = (
            {
                "ok": bool(capability_probes) and all(capability_probes.values()),
                "probes": capability_probes,
                "passed": sum(1 for v in capability_probes.values() if v),
                "total": len(capability_probes),
                "note": "supplementary — does not gate ready",
            }
            if capability_probes
            else {"ok": False, "probes": {}, "note": "not probed (probe round-trip failed)"}
        )

        # 5. Login state — inferred from URL/title vs login-wall heuristics.
        url = (obs_data.get("url") or "").lower()
        title = (obs_data.get("title") or "").lower()
        login_wall = _is_login_wall(url, title)
        diag["checks"]["login_state"] = {
            "known": True,
            "authenticated": not login_wall and nav_ok,
            "login_wall_detected": login_wall,
        }

        # Overall readiness (2026-09-22 policy): X reachable + observe works +
        # login state known + ALL CORE PROBES pass. A stale-DOM system must
        # report ready=false — the prior majority-vote policy reported ready
        # while 3/5 probes failed, defeating the diagnostic's purpose.
        core_passed = all(
            diag["checks"]["selector_readiness"]["probes"].get(p, False)
            for p in _CORE_PROBES
        ) if "probes" in diag["checks"]["selector_readiness"] else False
        diag["checks"]["selector_readiness"]["core_passed"] = core_passed
        ready = bool(
            nav_ok
            and diag["checks"]["observe"]["ok"]
            and diag["checks"]["login_state"]["known"]
            and core_passed
        )
        diag["ready"] = ready

        if ready:
            return ok_result(data=diag, success_category=SuccessCategory.INSPECTION)
        # Distinguish auth vs infrastructure failure.
        if login_wall:
            from webwire.envelope import auth_required
            diag["ready"] = False
            r = auth_required("Login wall detected — not authenticated.")
            r.data = diag
            return r
        r = soft_failure(
            "Health checks failed — see diagnostic data.",
            failure_category=FailureCategory.UNKNOWN,
        )
        r.data = diag  # the diagnostic IS the payload of a failed health check
        return r


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _is_login_wall(url: str, title: str) -> bool:
    frags_url = ("/login", "/i/flow/login", "oauth/authorize")
    frags_title = ("log in", "sign in")
    return any(f in url for f in frags_url) or any(f in title for f in frags_title)


# Hydration poll: X renders client-side after domcontentloaded; selector
# probes before render return false for everything. Poll until the
# main-content probe passes, with a deadline. Polling beats a fixed sleep:
# fast when warm, bounded when slow (2026-09-22, replaces fixed-sleep drift).
_HYDRATION_TIMEOUT_S = 8.0
_HYDRATION_POLL_INTERVAL_S = 1.0


async def _poll_dom_probes(broker: ReadOnlyBroker) -> tuple[dict[str, bool], dict[str, bool]]:
    """Poll probe_selectors (shell set + capability set in ONE round-trip)
    until BOTH sets pass or deadline. Returns (shell, capability) probe
    dicts — empty dicts if the round-trip never succeeded (caller falls back
    to observation heuristics). Two race lessons encoded: (1) exiting on the
    main probe alone races the side nav (2026-09-22); (2) exiting on the
    SHELL set alone races the FEED — app chrome renders before feed articles,
    so capability selectors (tweetText/bookmark per article) probe false on a
    healthy-but-slow feed (caught live 2026-09-23). Both sets are waited for;
    only the SHELL set gates `ready` — the capability set stays supplementary."""
    import asyncio
    import time

    merged = {**_PROBE_SELECTOR_SET, **_CAPABILITY_SELECTOR_SET}
    shell_keys = set(_PROBE_SELECTOR_SET)
    deadline = time.monotonic() + _HYDRATION_TIMEOUT_S
    probes: dict[str, bool] = {}
    attempts = 0
    while True:
        attempts += 1
        r = await broker.probe_selectors(merged)
        if r.ok:
            probes = (r.data or {}).get("probes", {})
            if probes and all(probes.values()):
                shell = {k: v for k, v in probes.items() if k in shell_keys}
                cap = {k: v for k, v in probes.items() if k not in shell_keys}
                return shell, cap
        if time.monotonic() >= deadline:
            if not probes:
                logger.warning(
                    "probe_selectors failed %d attempts; falling back to observation",
                    attempts,
                )
            shell = {k: v for k, v in probes.items() if k in shell_keys}
            cap = {k: v for k, v in probes.items() if k not in shell_keys}
            return shell, cap
        await asyncio.sleep(_HYDRATION_POLL_INTERVAL_S)


def _probe_readiness(obs_data: dict[str, Any], dom_probes: dict[str, bool]) -> dict[str, Any]:
    """Run the probe set. DOM-direct probes take precedence; the observe()
    heuristics serve as fallback for each probe the DOM round-trip couldn't
    answer. Strict aggregation: ok requires ALL probes (it is the DOM-drift
    detector); `ready` gating is decided by the caller on the core subset."""
    url = (obs_data.get("url") or "").lower()
    title = (obs_data.get("title") or "").lower()
    targets = obs_data.get("targets", []) or []
    roles = {t.get("role") for t in targets if isinstance(t, dict)}

    def _dom(name: str) -> Optional[bool]:
        v = dom_probes.get(name)
        return v if isinstance(v, bool) else None

    dom_main = _dom("main_landmark_or_content")
    dom_nav = _dom("navigation_affordance")
    dom_acc = _dom("account_affordance")

    probes = {
        # URL on expected X surface
        "on_x_surface": url.startswith(("https://x.com/", "https://twitter.com/")),
        # login wall absent
        "login_wall_absent": not _is_login_wall(url, title),
        # a main/content landmark present (DOM probe; legacy: role=main or
        # a populated target snapshot)
        "main_landmark_or_content": (
            dom_main if dom_main is not None
            else (("main" in roles) or len(targets) >= 5)
        ),
        # some navigation affordance present
        "navigation_affordance": (
            dom_nav if dom_nav is not None
            else bool(roles & {"link", "menuitem", "tab"})
        ),
        # a profile-like / account affordance present
        "account_affordance": (
            dom_acc if dom_acc is not None
            else any(
                (t.get("role") == "link") and (t.get("name") or "").strip()
                for t in targets if isinstance(t, dict)
            )
        ),
    }
    passed = sum(1 for v in probes.values() if v)
    return {
        "ok": passed == len(probes),  # strict — this IS the drift detector
        "probes": probes,
        "passed": passed,
        "total": len(probes),
        "basis": "dom_probes" if any(isinstance(v, bool) for v in (
            dom_main, dom_nav, dom_acc,
        )) else "observation_fallback",
    }
