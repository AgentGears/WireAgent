"""Agent-WebWire — browser-native X/Twitter capability layer for AI agents.

Phase 0a: session + identity + health + envelope (reused ActionResult) + journal
+ kill switch + read-only browser-action boundary. No write capabilities exist.
"""

from webwire.config import ScreenshotPolicy, WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import (
    ActionError,
    ActionResult,
    FailureCategory,
    SuccessCategory,
)
from webwire.safety import KillSwitch
from webwire.session import SessionManager

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
