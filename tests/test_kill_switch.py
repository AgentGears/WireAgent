"""Tests for the kill switch — flag, hot file, reset, guard, boot env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import kill_switched
from webwire.safety import KillSwitch


def test_kill_switch_clean_by_default(tmp_path: Path) -> None:
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    assert not ks.tripped()
    assert ks.guard() is None


def test_kill_switch_trip_via_flag(tmp_path: Path) -> None:
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    ks.trip()
    assert ks.tripped()
    # Hot file should also be created so external observers see it.
    assert (tmp_path / "kill").exists()
    g = ks.guard()
    assert g is not None
    assert g.ok is False
    assert g.failure_category.value == "security"


def test_kill_switch_trip_via_hot_file(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    (tmp_path / "kill").touch()  # external trip
    ks = KillSwitch(cfg)
    assert ks.tripped()


def test_kill_switch_reset_clears_both(tmp_path: Path) -> None:
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    ks.trip()
    assert ks.tripped()
    ks.reset()
    assert not ks.tripped()
    assert not (tmp_path / "kill").exists()


def test_kill_switch_reset_without_file_keeps_external_trip(tmp_path: Path) -> None:
    """If the hot file was created externally and we only reset the flag,
    the switch must stay tripped (fail closed)."""
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    (tmp_path / "kill").touch()
    ks = KillSwitch(cfg)
    ks._flag = False  # reset only the in-process flag
    assert ks.tripped()  # hot file still trips it


def test_kill_switch_boot_env_var_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBWIRE_KILL_TEST", "1")
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var="WEBWIRE_KILL_TEST"))
    assert ks.tripped()


def test_kill_switch_boot_env_var_false_does_not_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBWIRE_KILL_TEST", "0")
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var="WEBWIRE_KILL_TEST"))
    assert not ks.tripped()


def test_kill_switch_state_snapshot(tmp_path: Path) -> None:
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    ks.trip()
    state = ks.state()
    assert state["tripped"] is True
    assert state["flag"] is True
    assert state["hot_file_exists"] is True


def test_kill_switch_returns_kill_switched_envelope(tmp_path: Path) -> None:
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    ks.trip()
    g = ks.guard()
    assert g is not None
    # Same shape as the envelope constructor.
    expected = kill_switched()
    assert g.failure_category == expected.failure_category
    assert g.ok is False
