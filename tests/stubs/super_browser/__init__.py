"""Offline stub of the Super-Browser SDK (CI-only).

Activates ONLY when the real `super_browser` package is not importable
(tests/conftest.py guards this). Tests/parity is enforced against the real
SDK on machines where it IS installed (tests/test_stub_parity.py) — if the
real API drifts, that test fails locally first.

Every SuperBrowser method raises NotImplementedError if actually invoked:
offline tests must never touch a real browser path silently; a stubbed-out
live call should fail loudly, not pass vacuously.
"""

from super_browser.browser.config import Config, SessionConfig, SessionMode


class SuperBrowser:
    """Stub facade. Constructible; every method is a loud failure."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config
        self._controller = None

    def _not_implemented(self, *args, **kwargs):
        raise NotImplementedError(
            "SuperBrowser stub: live browser paths are unavailable in offline "
            "tests/CI. If you see this in a LIVE run, the stub leaked into a "
            "real environment — install the SDK: pip install -e '.[browser]'"
        )

    start = stop = navigate = reload = go_back = go_forward = _not_implemented
    observe = extract = click = act = fill = delegate = upload_file = _not_implemented
    download = check = uncheck = type_text = list_tabs = switch_tab = _not_implemented
    save_session = load_session = _not_implemented


__all__ = ["Config", "SessionConfig", "SessionMode", "SuperBrowser"]
