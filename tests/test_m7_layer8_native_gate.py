"""Layer 8 — E-1 gate negative controls.

The gate's caption check is a pure function; these controls prove it FAILS
on wrong-edition inputs (Windows 11, Windows 10, Server 2019, Server 2022)
and passes on the qualified edition's caption shapes. They run on every
platform — the qualified refusal behavior must not depend on being executed
on the qualified machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _l8_native_gate import caption_is_windows_server_2025  # noqa: E402


def test_gate_accepts_server_2025_caption_shapes() -> None:
    assert caption_is_windows_server_2025("Microsoft Windows Server 2025 Datacenter")
    assert caption_is_windows_server_2025("Microsoft Windows Server 2025 Standard")
    assert caption_is_windows_server_2025("Microsoft Windows Server 2025 Datacenter (Azure Edition) ")
    assert caption_is_windows_server_2025("microsoft windows server 2025 standard")


def test_gate_refuses_wrong_editions() -> None:
    # The documented windows-2025-vs2026 image reports platform strings that
    # differ per Python version; the CLIENT captions here are exactly the
    # strings a wrongly-pinned label could produce.
    assert not caption_is_windows_server_2025("Microsoft Windows 11 Pro")
    assert not caption_is_windows_server_2025("Microsoft Windows 10 Enterprise")
    assert not caption_is_windows_server_2025("Microsoft Windows Server 2022 Datacenter")
    assert not caption_is_windows_server_2025("Microsoft Windows Server 2019 Standard")
    assert not caption_is_windows_server_2025("Microsoft Windows Server 2026 Datacenter")


def test_gate_refuses_degenerate_inputs() -> None:
    assert not caption_is_windows_server_2025("")
    assert not caption_is_windows_server_2025("   ")
