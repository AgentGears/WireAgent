"""Agent-WebWire — browser-native X/Twitter capability layer for AI agents.

Phase 0a: session + identity + health + envelope (reused ActionResult) + journal
+ kill switch + read-only browser-action boundary. No write capabilities exist.

Imports are LAZY (PEP 562, F-44): importing a submodule such as
``webwire.safety.m8_rule_lifecycle`` no longer transitively imports the
dispatcher (and through it the browser SDK). The browser-dependent names
below resolve on first attribute access exactly as before.
"""

from typing import Any

__version__ = "0.0.1"

__all__ = [
    "Dispatcher",
    "SessionManager",
    "WebWireConfig",
    "ScreenshotPolicy",
    "KillSwitch",
    "ActionResult",
    "ActionError",
    "FailureCategory",
    "SuccessCategory",
]

_LAZY_IMPORTS = {
    "Dispatcher": ("webwire.dispatcher", "Dispatcher"),
    "SessionManager": ("webwire.session", "SessionManager"),
    "WebWireConfig": ("webwire.config", "WebWireConfig"),
    "ScreenshotPolicy": ("webwire.config", "ScreenshotPolicy"),
    "KillSwitch": ("webwire.safety", "KillSwitch"),
    "ActionResult": ("webwire.envelope", "ActionResult"),
    "ActionError": ("webwire.envelope", "ActionError"),
    "FailureCategory": ("webwire.envelope", "FailureCategory"),
    "SuccessCategory": ("webwire.envelope", "SuccessCategory"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        import importlib

        module_name, attr = _LAZY_IMPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_IMPORTS.keys()))
