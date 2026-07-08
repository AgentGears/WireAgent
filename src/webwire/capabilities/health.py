"""health capability — probe-set diagnostic, not a universal hard gate.

Per the Phase 0a design (Point 5 decision):
- Checks: browser session alive, X reachable, login state known, whoami can
  resolve identity OR returns AUTH_REQUIRED, kill-switch state, read-only broker
  policy active, selector/readiness probes.
- Selector readiness is a DIAGNOSTIC field using a small probe set with
  alternatives — NOT one brittle data-testid. Missing probes are not a universal
  hard failure unless they prevent identity/session determination.
- Returns a structured diagnostic blob; ``ok`` reflects overall readiness but
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

        # 4. Readiness probes — diagnostic, multiple alternatives, not a single testid.
        if obs_data:
            diag["checks"]["selector_readiness"] = _probe_readiness(obs_data)
        else:
            diag["checks"]["selector_readiness"] = {"ok": False, "probes": {}, "note": "no observation"}

        # 5. Login state — inferred from URL/title vs login-wall heuristics.
        url = (obs_data.get("url") or "").lower()
        title = (obs_data.get("title") or "").lower()
        login_wall = _is_login_wall(url, title)
        diag["checks"]["login_state"] = {
            "known": True,
            "authenticated": not login_wall and nav_ok,
            "login_wall_detected": login_wall,
        }

        # Overall readiness: X reachable + observe works + login state known.
        # Selector-probe failures do NOT make health fail (they're diagnostic),
        # UNLESS observation itself failed (then we can't determine anything).
        ready = bool(
            nav_ok
            and diag["checks"]["observe"]["ok"]
            and diag["checks"]["login_state"]["known"]
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
        return soft_failure(
            "Health checks failed — see diagnostic data.",
            failure_category=FailureCategory.UNKNOWN,
        )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _is_login_wall(url: str, title: str) -> bool:
    frags_url = ("/login", "/i/flow/login", "oauth/authorize")
    frags_title = ("log in", "sign in")
    return any(f in url for f in frags_url) or any(f in title for f in frags_title)


def _probe_readiness(obs_data: dict[str, Any]) -> dict[str, Any]:
    """Run a small probe set with alternatives. Diagnostic, not a hard gate."""
    url = (obs_data.get("url") or "").lower()
    title = (obs_data.get("title") or "").lower()
    targets = obs_data.get("targets", []) or []
    roles = {t.get("role") for t in targets if isinstance(t, dict)}

    probes = {
        # URL on expected X surface
        "on_x_surface": url.startswith(("https://x.com/", "https://twitter.com/")),
        # login wall absent
        "login_wall_absent": not _is_login_wall(url, title),
        # a main/content landmark present (role=main or large target count)
        "main_landmark_or_content": ("main" in roles) or len(targets) >= 5,
        # some navigation affordance present
        "navigation_affordance": bool(roles & {"link", "menuitem", "tab"}),
        # a profile-like / account affordance (link target present)
        "account_affordance": any(
            (t.get("role") == "link") and (t.get("name") or "").strip()
            for t in targets if isinstance(t, dict)
        ),
    }
    passed = sum(1 for v in probes.values() if v)
    return {
        "ok": passed >= 3,  # majority of probes — tolerant
        "probes": probes,
        "passed": passed,
        "total": len(probes),
    }
