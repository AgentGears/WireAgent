"""Stub of super_browser.browser.config — field-derived from the real SDK
(parity-enforced locally). Every SessionConfig field the real SDK defines is
present so dataclass-field parity holds when the real SDK gains fields and
the parity test flags the drift."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class SessionMode(StrEnum):
    PATCHRIGHT_LAUNCH = "patchright_launch"
    PATCHRIGHT_ATTACH = "patchright_attach"


@dataclass
class SessionConfig:
    mode: SessionMode = SessionMode.PATCHRIGHT_LAUNCH
    headless: bool = False
    executable_path: Optional[str] = None
    chrome_args: tuple[str, ...] = ()
    user_data_dir: Optional[str] = None
    proxy: Optional[str] = None
    viewport: tuple[int, int] = (1280, 720)
    user_agent: Optional[str] = None
    locale: Optional[str] = "en-US"
    default_timeout: float = 30.0
    navigation_timeout: float = 30.0
    discovery_timeout: float = 30.0
    discovery_interval: float = 0.5
    cdp_ws_url: Optional[str] = None
    daemon_socket_path: Optional[str] = None
    stale_recovery: bool = True
    event_buffer_size: int = 500
    shutdown_grace_period: float = 7.0
    backend: str = "auto"
    browser_type: str = "chromium"
    endpoint: str = ""
    session_file: Optional[str] = None


@dataclass
class _AgentConfig:
    enable_recovery: bool = True
    enable_budget: bool = True
    enable_security: bool = True


@dataclass
class Config:
    browser: SessionConfig
    agent: _AgentConfig = None  # type: ignore[assignment]  (set in __post_init__)


def _config_init(self, browser: SessionConfig | None = None, **kwargs) -> None:
    self.browser = browser or SessionConfig()
    self.agent = _AgentConfig()
    for k, v in kwargs.items():
        setattr(self, k, v)


Config.__init__ = _config_init  # type: ignore[assignment]
