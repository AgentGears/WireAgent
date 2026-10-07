"""M7 Layer 5 — reconciliation routes through the active owner (frozen
§14.1 / M7-T39).

While the production owner is alive, an external recovery/operator
process either routes through the owner IPC boundary or fails
authority_busy; it cannot construct a parallel write-capable M6
coordinator for the same authority domain. Owner-absent standalone
recovery remains the existing temporary-owner path (Layer 3's
OfflineRecoveryAuthority).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.authority import AuthorityOwnerLock
from webwire.config import WebWireConfig


def test_external_recovery_under_live_owner_fails_busy_before_m6(tmp_path: Path) -> None:
    """M7-T39: with the owner lock held (a live production owner), an
    external recovery process cannot acquire, cannot construct any
    write-capable M6 coordinator, and surfaces authority_busy — routing
    through the owner is the only write path."""
    from webwire.offline_recovery import OfflineRecoveryAuthority

    owner = AuthorityOwnerLock(tmp_path).acquire()
    try:
        rival = OfflineRecoveryAuthority(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
        with pytest.raises(Exception) as excinfo:
            rival.acquire()
        message = str(excinfo.value).lower()
        assert "busy" in message or "another" in message, message
        # No parallel coordinator was constructed: the recovery owner
        # holds nothing after the failed acquisition.
        assert rival._coordinator is None
        assert rival._owner_lock is None
        assert rival._session is None
    finally:
        owner.release()


def test_owner_absent_standalone_recovery_remains_the_temporary_owner(tmp_path: Path) -> None:
    """§14.2: with NO live owner, the standalone recovery program still
    acquires the same lock and becomes the temporary authority owner
    (the existing Layer-3 path, unchanged by Layer 5)."""
    from webwire.offline_recovery import OfflineRecoveryAuthority

    recovery = OfflineRecoveryAuthority(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    acquired = recovery.acquire()
    try:
        assert acquired is recovery
        assert recovery._owner_lock is not None
        assert recovery._session is not None
        # And the production-side claim is exclusive: a second acquisition
        # (owner or recovery) is refused while the temporary owner lives.
        from webwire.offline_recovery import OfflineRecoveryAuthority as Second

        with pytest.raises(Exception, match="busy|another"):  # noqa: B017
            Second(WebWireConfig(state_dir=tmp_path, kill_env_var=None)).acquire()
    finally:
        recovery.close()
