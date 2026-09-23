"""Re-entrant KillSwitch listener regressions for M5 layer 3."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.safety.kill_switch import KillSwitch


def _kill(tmp_path: Path) -> KillSwitch:
    return KillSwitch(WebWireConfig(state_dir=tmp_path))


def test_listener_can_call_tripped_without_recursive_reinvocation(tmp_path: Path) -> None:
    """Codex P2: re-entrant observation must not recursively invoke itself."""
    kill = _kill(tmp_path)
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1
        assert kill.tripped() is True

    kill.add_trip_listener(listener)
    kill.trip()

    assert calls == 1
    assert kill.tripped() is True
    assert calls == 1


def test_listener_can_call_trip_without_recursive_reinvocation(tmp_path: Path) -> None:
    """A nested trip observes the in-progress marker and does not recurse."""
    kill = _kill(tmp_path)
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1
        kill.trip()

    kill.add_trip_listener(listener)
    kill.trip()

    assert calls == 1
    assert kill.tripped() is True
    assert calls == 1


def test_listener_can_add_listener_during_notification(tmp_path: Path) -> None:
    """Re-entrant registration skips the current callback and notifies the new one."""
    kill = _kill(tmp_path)
    first_calls = 0
    second_calls = 0

    def second() -> None:
        nonlocal second_calls
        second_calls += 1

    def first() -> None:
        nonlocal first_calls
        first_calls += 1
        kill.add_trip_listener(second)

    kill.add_trip_listener(first)
    kill.trip()

    assert first_calls == 1
    assert second_calls == 1
    assert kill.tripped() is True
    assert first_calls == 1
    assert second_calls == 1


def test_failed_reentrant_listener_remains_retryable_without_recursion(tmp_path: Path) -> None:
    """Failure clears only in-progress state; later observation retries once."""
    kill = _kill(tmp_path)
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1
        assert kill.tripped() is True
        raise RuntimeError("injected listener failure")

    kill.add_trip_listener(listener)
    kill.trip()
    assert calls == 1

    # The failed callback was not marked notified, so a later observation
    # retries it exactly once rather than recursively re-entering it.
    assert kill.tripped() is True
    assert calls == 2


def test_listener_reset_stops_remaining_delivery_for_inactive_trip(tmp_path: Path) -> None:
    """If re-entry resets the switch, later listeners are not called for that trip."""
    kill = _kill(tmp_path)
    first_calls = 0
    second_calls = 0

    def first() -> None:
        nonlocal first_calls
        first_calls += 1
        kill.reset()

    def second() -> None:
        nonlocal second_calls
        second_calls += 1

    kill.add_trip_listener(first)
    kill.add_trip_listener(second)
    kill.trip()

    assert first_calls == 1
    assert second_calls == 0
    assert kill.tripped() is False


def test_listener_reset_then_retrip_is_notified_for_both_generations(tmp_path: Path) -> None:
    """Codex P2: an old callback cannot satisfy a newly-created trip generation."""
    kill = _kill(tmp_path)
    calls = 0

    def listener() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            kill.reset()
            kill.trip()

    kill.add_trip_listener(listener)
    kill.trip()

    # The first callback belongs to generation 1. reset()+trip() creates
    # generation 2 while it is in progress, so generation 2 must receive its
    # own callback after the first invocation exits.
    assert calls == 2
    assert kill.tripped() is True
    assert calls == 2
