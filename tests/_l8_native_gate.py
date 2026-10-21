"""Layer 8 — native Windows Server 2025 identity gate (E-1).

The gate reads the OS PRODUCT IDENTITY from a native mechanism (the WMI
`Win32_OperatingSystem` caption via PowerShell — NOT a `platform.platform()`
substring, which diverges across Python versions on the same image), and
refuses to pass on any other edition. Its negative controls live in
`tests/test_m7_layer8_native_gate.py`: the caption CHECK is a pure function
exercised against wrong-edition inputs, so the gate's refusal behavior is
itself qualified rather than assumed.
"""

from __future__ import annotations

import subprocess
import sys

REQUIRED_MARKER = "server 2025"


def caption_is_windows_server_2025(caption: str) -> bool:
    """Pure check: the native OS product caption names Windows Server 2025.

    Case-insensitive on the marker; deliberately tolerant of the SKU suffix
    (Datacenter/Standard/Azure Edition) and of leading/trailing whitespace,
    and deliberately intolerant of Windows 11/10 client captions, Server
    2019/2022, and anything else.
    """

    return REQUIRED_MARKER in (caption or "").strip().lower()


def read_native_caption() -> str:
    """The OS product caption through PowerShell's CIM query (native path)."""

    probe = subprocess.run(  # noqa: S603 - fixed command, no shell interpolation
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).Caption",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"native caption query failed: {probe.stderr[-200:]!r}")
    return probe.stdout.strip()


def main() -> int:
    caption = read_native_caption()
    print(f"L8-E1 native OS product caption: {caption!r}")
    if not caption_is_windows_server_2025(caption):
        print(
            "L8-E1 GATE REFUSED: the qualified environment is Windows Server 2025; "
            "this runner is a different edition. Qualification evidence from this "
            "job would be invalid.",
            file=sys.stderr,
        )
        return 1
    print("L8-E1 gate passed: Windows Server 2025 confirmed by native product identity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
