"""KillSwitch drain-shutdown handoff regressions for M5 layer 3."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.safety.kill_switch import KillSwitch


class _PauseAfterIdleDecisionLock:
    """RLock wrapper that pauses one named thread immediately after release.

    The test arms the pause only when the original drainer has selected no
    further callback. This deterministically opens the historical window
    between releasing ``_state_lock`` and clearing ``_notifying``.
    """

    def __init__(self, *, target_thread: str) -> None:
        self._inner = threading.RLock()
        self._target_thread = target_thread
        self.pause_next_release = threading.Event()
        self.released = threading.Event()
        self.resume = threading.Event()

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        return self._inner.acquire(*args, **kwargs)

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> _PauseAfterIdleDecisionLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
        if (
            threading.current_thread().name == self._target_thread
            and self.pause_next_release.is_set()
        ):
            self.pause_next_release.clear()
            self.released.set()
            if not self.resume.wait(timeout=5):
                raise AssertionError("timed out waiting to release paused drainer")


def _kill(tmp_path: Path) -> KillSwitch:
    return KillSwitch(WebWireConfig(state_dir=tmp_path))


def _install_idle_release_pause(
    kill: KillSwitch,
    *,
    target_thread: str = "kill-drainer",
) -> _PauseAfterIdleDecisionLock:
    """Pause the target drainer after selecting no pending listener event."""
    lock = _PauseAfterIdleDecisionLock(target_thread=target_thread)
    kill._state_lock = lock  # type: ignore[assignment]
    original_select = kill._select_pending_event_unlocked

    def select_with_pause(
        attempted: list[tuple[int, int]],
    ) -> tuple[int, int, object] | None:
        candidate = original_select(attempted)
        if candidate is None and threading.current_thread().name == target_thread:
            lock.pause_next_release.set()
        return candidate  # type: ignore[return-value]

    kill._select_pending_event_unlocked = select_with_pause  # type: ignore[method-assign]
    return lock


def test_late_listener_cannot_be_stranded_during_drain_shutdown(tmp_path: Path) -> None:
    """Codex P2: idle publication and the no-work decision are one handoff."""
    kill = _kill(tmp_path)
    seed_called = threading.Event()
    late_called = threading.Event()

    kill.add_trip_listener(seed_called.set)
    pause = _install_idle_release_pause(kill)

    drainer = threading.Thread(target=kill.trip, name="kill-drainer")
    drainer.start()
    assert pause.released.wait(timeout=5)
    assert seed_called.is_set()

    def late_listener() -> None:
        late_called.set()

    registrar = threading.Thread(
        target=lambda: kill.add_trip_listener(late_listener),
        name="late-registrar",
    )
    registrar.start()
    registrar.join(timeout=5)
    assert not registrar.is_alive()
    delivered_before_old_drainer_resumes = late_called.is_set()

    pause.resume.set()
    drainer.join(timeout=5)
    assert not drainer.is_alive()

    assert delivered_before_old_drainer_resumes is True
    assert late_called.is_set()


def test_new_trip_generation_cannot_be_stranded_during_drain_shutdown(
    tmp_path: Path,
) -> None:
    """A reset→retrip arriving at drain shutdown must start a fresh drain."""
    kill = _kill(tmp_path)
    first_delivery = threading.Event()
    second_delivery = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def listener() -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            first_delivery.set()
        elif current == 2:
            second_delivery.set()

    kill.add_trip_listener(listener)
    pause = _install_idle_release_pause(kill)

    drainer = threading.Thread(target=kill.trip, name="kill-drainer")
    drainer.start()
    assert pause.released.wait(timeout=5)
    assert first_delivery.is_set()

    def retrip() -> None:
        kill.reset()
        kill.trip()

    retripper = threading.Thread(target=retrip, name="retrip-thread")
    retripper.start()
    retripper.join(timeout=5)
    assert not retripper.is_alive()
    delivered_before_old_drainer_resumes = second_delivery.is_set()

    pause.resume.set()
    drainer.join(timeout=5)
    assert not drainer.is_alive()

    assert delivered_before_old_drainer_resumes is True
    assert second_delivery.is_set()
    assert calls == 2
