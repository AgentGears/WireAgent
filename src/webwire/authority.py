"""M7 Layer 1 cross-process authority ownership primitive.

This module implements only the outer ownership mechanism frozen by
``docs/M7_DESIGN.md`` Layer 1:

- canonicalize one configured local ``state_dir`` into an authority domain;
- reserve that domain in one process-local registry before OS lock acquisition;
- acquire one non-expiring, non-blocking OS-held owner lock on
  ``authority.lock``;
- keep the dedicated owner descriptor private and non-inheritable across
  qualified spawn/exec paths;
- leave the rendezvous file in place across release/crash;
- fail closed on acquisition/release errors.

It deliberately does **not** create AuthoritySession, service lifecycle, browser,
recovery, or IPC authority. Those are later M7 layers.
"""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import Any, ClassVar, Optional

__all__ = [
    "AuthorityBusyError",
    "AuthorityOwnerError",
    "AuthorityOwnerLock",
    "AuthorityStateError",
    "canonical_authority_domain",
]


class AuthorityOwnerError(RuntimeError):
    """The M7 owner-lock mechanism could not establish or release authority safely."""


class AuthorityBusyError(AuthorityOwnerError):
    """Another supported owner currently holds/reserves this authority domain."""


class AuthorityStateError(AuthorityOwnerError):
    """The caller attempted an invalid owner-lock lifecycle transition."""


def canonical_authority_domain(state_dir: Path) -> Path:
    """Resolve one configured state directory into the M7 authority-domain path.

    ``resolve(strict=False)`` intentionally mirrors the path-identity posture
    already used by the M5/M6 process-local registries while preserving the real
    absolute path for diagnostics and the rendezvous file. Case folding is used
    only for registry identity, not by rewriting the returned filesystem path.
    """

    try:
        return Path(state_dir).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AuthorityOwnerError(
            f"could not canonicalize authority domain {state_dir!s}: {exc!r}"
        ) from exc


class AuthorityOwnerLock:
    """Dedicated non-expiring OS lock for one canonical M7 authority domain.

    The process-local registry is acquired *before* the OS primitive so two
    accidental authority roots inside one interpreter cannot both proceed even
    on platforms with surprising same-process file-lock behavior. The registry
    stores the active object strongly: dropping the caller's last reference does
    not silently release production ownership.

    A rendezvous file is never ownership by itself and is intentionally retained
    after release. No PID, mtime, heartbeat, or TTL participates in authority.
    """

    _registry_guard: ClassVar[Any] = threading.Lock()
    _registry: ClassVar[dict[str, "AuthorityOwnerLock"]] = {}

    def __init__(self, state_dir: Path) -> None:
        self._authority_domain = canonical_authority_domain(state_dir)
        self._identity = os.path.normcase(str(self._authority_domain))
        self._lock_path = self._authority_domain / "authority.lock"
        self._state_lock: Any = threading.RLock()
        self._fd: Optional[int] = None
        self._owner_pid: Optional[int] = None
        self._release_broken = False

    @property
    def authority_domain(self) -> Path:
        return self._authority_domain

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def held(self) -> bool:
        """Whether this object currently owns the lock in this process."""

        return (
            self._fd is not None
            and self._owner_pid == os.getpid()
            and not self._release_broken
        )

    @classmethod
    def _reserve_process_domain(cls, owner: "AuthorityOwnerLock") -> None:
        with cls._registry_guard:
            current = cls._registry.get(owner._identity)
            if current is not None:
                raise AuthorityBusyError(
                    f"authority_busy: domain {owner.authority_domain} is already "
                    "reserved by this process"
                )
            cls._registry[owner._identity] = owner

    @classmethod
    def _release_process_domain(cls, owner: "AuthorityOwnerLock") -> None:
        with cls._registry_guard:
            current = cls._registry.get(owner._identity)
            if current is owner:
                del cls._registry[owner._identity]
                return
            raise AuthorityOwnerError(
                "authority owner registry lost the active domain reservation"
            )

    @classmethod
    def _rollback_process_domain(cls, owner: "AuthorityOwnerLock") -> None:
        with cls._registry_guard:
            if cls._registry.get(owner._identity) is owner:
                del cls._registry[owner._identity]

    def _open_lock_file(self) -> int:
        self._authority_domain.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.set_inheritable(fd, False)
        return fd

    @staticmethod
    def _lock_fd(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                # Locking may extend beyond EOF, so the rendezvous file does not
                # need mutable sentinel content. LK_NBLCK is genuinely
                # non-blocking; contention is reported as OSError.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                    errno.EWOULDBLOCK,
                }:
                    raise AuthorityBusyError("authority_busy: OS owner lock held") from exc
                raise
            return

        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if isinstance(exc, BlockingIOError) or exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                }:
                    raise AuthorityBusyError("authority_busy: OS owner lock held") from exc
                raise
            return

        raise OSError(errno.ENOSYS, f"unsupported owner-lock platform os.name={os.name!r}")

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return

        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
            return

        raise OSError(errno.ENOSYS, f"unsupported owner-lock platform os.name={os.name!r}")

    @staticmethod
    def _close_noexcept(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def acquire(self) -> "AuthorityOwnerLock":
        """Acquire this authority domain immediately or fail ``authority_busy``.

        There is no blocking wait, TTL, stale-PID check, or stale-file deletion.
        """

        with self._state_lock:
            if self._release_broken:
                raise AuthorityStateError(
                    "owner lock entered an indeterminate release state; terminate "
                    "the process instead of reusing it"
                )
            if self._fd is not None:
                raise AuthorityStateError("owner lock is already acquired")

            self._reserve_process_domain(self)
            fd: Optional[int] = None
            try:
                fd = self._open_lock_file()
                # Publish the private descriptor before entering the OS call so a
                # POSIX fork callback can detach it even if another thread forks
                # while this thread is inside acquisition.
                self._fd = fd
                self._owner_pid = os.getpid()
                self._lock_fd(fd)
            except AuthorityBusyError:
                if fd is not None:
                    self._close_noexcept(fd)
                self._fd = None
                self._owner_pid = None
                self._rollback_process_domain(self)
                raise
            except (OSError, AuthorityOwnerError) as exc:
                if fd is not None:
                    self._close_noexcept(fd)
                self._fd = None
                self._owner_pid = None
                self._rollback_process_domain(self)
                if isinstance(exc, AuthorityOwnerError):
                    raise
                raise AuthorityOwnerError(
                    f"could not acquire authority owner lock {self._lock_path}: {exc!r}"
                ) from exc
            except Exception:
                if fd is not None:
                    self._close_noexcept(fd)
                self._fd = None
                self._owner_pid = None
                self._rollback_process_domain(self)
                raise

            return self

    def release(self) -> None:
        """Release the OS owner lock without deleting the rendezvous file."""

        with self._state_lock:
            if self._release_broken:
                raise AuthorityStateError(
                    "owner lock release previously failed; terminate the process "
                    "instead of attempting ownership transfer"
                )
            if self._fd is None or self._owner_pid != os.getpid():
                raise AuthorityStateError("owner lock is not held by this process")

            fd = self._fd
            try:
                self._unlock_fd(fd)
            except OSError as exc:
                raise AuthorityOwnerError(
                    f"could not release authority owner lock {self._lock_path}: {exc!r}"
                ) from exc

            try:
                os.close(fd)
            except OSError as exc:
                # The OS unlock already succeeded, so this object can no longer
                # safely make a positive ownership claim. Keep the process-local
                # reservation to fail closed and require process termination.
                self._release_broken = True
                raise AuthorityOwnerError(
                    f"owner-lock descriptor close failed after unlock: {exc!r}"
                ) from exc

            self._fd = None
            self._owner_pid = None
            self._release_process_domain(self)

    def __enter__(self) -> "AuthorityOwnerLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    @classmethod
    def _after_fork_child(cls) -> None:
        """Detach inherited owner descriptors in a POSIX fork child.

        ``flock`` ownership may be associated with an inherited open-file
        description. Calling explicit unlock in the child could therefore drop
        the parent's lock. The child closes only its inherited descriptor copy,
        clears the copied process-local registry, and gets fresh thread locks.
        Full fork semantics remain a later M7 platform-qualification claim.
        """

        inherited = list(cls._registry.values())
        for owner in inherited:
            fd = owner._fd
            if fd is not None:
                cls._close_noexcept(fd)
            owner._fd = None
            owner._owner_pid = None
            owner._release_broken = False
            owner._state_lock = threading.RLock()
        cls._registry = {}
        cls._registry_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=AuthorityOwnerLock._after_fork_child)
