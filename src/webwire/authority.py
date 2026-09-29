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
- end ownership by deliberate private-handle close, not a pre-close unlock;
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
    # POSIX fork must never snapshot a descriptor during an interval where its
    # integer exists in the process descriptor table but the child-detach hook
    # cannot identify whether that integer still belongs to this owner. The same
    # gate therefore protects both publication after open and unpublication after
    # close. The at-fork prepare callback acquires it before every supported fork.
    _fork_guard: ClassVar[Any] = threading.Lock()
    # If descriptor close/unpublication becomes ambiguous, a later fork child
    # must not continue with copied state or inspect stale descriptor integers.
    # The child exits fail-stop before any selective owner-fd cleanup.
    _BROKEN_FORK_EXIT_CODE: ClassVar[int] = 70
    # A Python-level raising signal can theoretically arrive after the kernel
    # returns a newly opened integer descriptor but before bytecode stores that
    # integer anywhere Python cleanup can discover. There is no safe selective
    # cleanup at that seam. The supported response is fail-stop process exit;
    # process death closes the hidden descriptor before replacement is possible.
    _HIDDEN_OPEN_EXIT_CODE: ClassVar[int] = 71

    def __init__(self, state_dir: Path) -> None:
        self._authority_domain = canonical_authority_domain(state_dir)
        self._identity = os.path.normcase(str(self._authority_domain))
        self._lock_path = self._authority_domain / "authority.lock"
        self._state_lock: Any = threading.RLock()
        self._pending_fd: Optional[int] = None
        self._fd: Optional[int] = None
        self._owner_pid: Optional[int] = None
        self._release_broken = False
        self._opening_unpublished = False

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
    def _rollback_process_domain(cls, owner: "AuthorityOwnerLock") -> None:
        with cls._registry_guard:
            if cls._registry.get(owner._identity) is owner:
                del cls._registry[owner._identity]

    def _open_lock_file(self) -> int:
        self._authority_domain.mkdir(parents=True, exist_ok=True)
        # os.open() may release the GIL. A sibling thread could otherwise fork
        # after the kernel installs the fd but before Python publishes it on this
        # object, leaving an inherited open-file description invisible to the
        # child-detach hook. The at-fork prepare callback acquires _fork_guard,
        # so no supported os.fork() can cross this open/publication window.
        with self._fork_guard:
            # Mark the only interval in which the kernel may have created an fd
            # whose integer has not yet reached a Python local/object field. A
            # normal os.open OSError proves no descriptor was returned and clears
            # the marker. Any other BaseException with no published fd forces the
            # outer acquisition path to fail-stop the process rather than leak an
            # unknowable descriptor.
            self._opening_unpublished = True
            try:
                fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                self._opening_unpublished = False
                raise
            self._pending_fd = fd
            self._opening_unpublished = False

        try:
            # CPython opens descriptors non-inheritable by default, but repeat
            # the requirement explicitly at the authority boundary. Once the fd
            # is published as pending, a POSIX fork child can always discover and
            # close its inherited copy even if this hardening call is in flight.
            os.set_inheritable(fd, False)
            # Keep the return itself inside the protected try. A caught async
            # interruption between hardening and return must run the same cleanup
            # instead of leaving a pending fd that the caller never received.
            return fd
        except BaseException as exc:
            try:
                self._close_and_unpublish_fd(fd, phase="pending owner handle")
            except AuthorityOwnerError as close_exc:
                raise close_exc from exc
            raise

    @staticmethod
    def _lock_fd(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            # Linux typeshed intentionally omits the Windows-only locking API,
            # while Ruff rejects constant getattr() calls. Resolve through the
            # runtime module dictionary so static analysis remains portable and
            # a Windows runtime missing the CRT members still fails closed.
            crt = vars(msvcrt)
            locking = crt.get("locking")
            lk_nblck = crt.get("LK_NBLCK")
            if not callable(locking) or not isinstance(lk_nblck, int):
                raise OSError(
                    errno.ENOSYS,
                    "Windows CRT does not expose non-blocking file-region locking",
                )

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                # Locking may extend beyond EOF, so the rendezvous file does not
                # need mutable sentinel content. LK_NBLCK is genuinely
                # non-blocking; contention is reported as OSError.
                locking(fd, lk_nblck, 1)
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
    def _close_owner_fd(fd: int) -> None:
        """Close a private owner descriptor at an ownership-state boundary."""

        os.close(fd)

    @staticmethod
    def _close_noexcept(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def _close_and_unpublish_fd(self, fd: int, *, phase: str) -> None:
        """Close one owner fd and clear every published reference atomically vs fork.

        A successful close can make ``fd`` immediately reusable by another
        thread. Keeping a stale integer in ``_pending_fd``/``_fd`` across a fork
        would make the child hook close an unrelated inherited resource. Hold the
        same gate used by the at-fork prepare callback across kernel close and
        owner-state unpublication.

        Any exception across close *or* unpublication is ownership ambiguity.
        Descriptor fields and the process-domain reservation remain fail-closed,
        and any later POSIX fork child terminates before inspecting those possibly
        stale integers. The caller must terminate the broken parent process rather
        than guess whether ownership survived.
        """

        with self._fork_guard:
            try:
                self._close_owner_fd(fd)
                if self._pending_fd == fd:
                    self._pending_fd = None
                if self._fd == fd:
                    self._fd = None
                    self._owner_pid = None
                self._opening_unpublished = False
            except BaseException as exc:
                # A signal/async exception can arrive after kernel close succeeds
                # but before Python clears the published fd field. Treat that
                # exactly like an OSError from close: the integer may already be
                # reusable, so never let a fork child selectively close it.
                self._release_broken = True
                raise AuthorityOwnerError(
                    f"could not close {phase} {self._lock_path}: {exc!r}; "
                    "owner descriptor state is ambiguous, terminate the process"
                ) from exc

    def _rollback_failed_acquisition(self, fd: Optional[int]) -> None:
        """Undo a definitely-clean acquisition failure or stay reserved on ambiguity."""

        if self._release_broken:
            # A lower layer already encountered ambiguous close state. Preserve
            # every discoverable descriptor reference and the registry slot.
            return

        # An asynchronous exception may land after _open_lock_file publishes its
        # pending descriptor but before the caller's STORE_FAST receives the
        # return value. Derive cleanup authority from the object fields as well as
        # the local variable so that bytecode seam cannot strand a descriptor.
        cleanup_fd = fd
        if cleanup_fd is None:
            cleanup_fd = self._pending_fd
        if cleanup_fd is None:
            cleanup_fd = self._fd
        if cleanup_fd is not None:
            self._close_and_unpublish_fd(
                cleanup_fd,
                phase="pending authority owner handle",
            )

        if self._pending_fd is not None or self._fd is not None:
            # Multiple/distinct owner descriptors are outside the invariant. Do
            # not free the domain if cleanup could not establish an empty owner
            # descriptor state.
            self._release_broken = True
            raise AuthorityOwnerError(
                "authority owner acquisition cleanup left ambiguous descriptor state"
            )

        self._opening_unpublished = False
        self._owner_pid = None
        self._rollback_process_domain(self)

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
            if (
                self._fd is not None
                or self._pending_fd is not None
                or self._opening_unpublished
            ):
                raise AuthorityStateError("owner lock is already acquired or acquiring")

            fd: Optional[int] = None
            try:
                # Reservation is part of the rollback-protected transaction. If
                # an async exception lands immediately after insertion, the
                # BaseException handler can remove this owner's strong registry
                # entry instead of stranding a descriptor-free busy domain.
                self._reserve_process_domain(self)
                fd = self._open_lock_file()
                self._lock_fd(fd)
                # The OS lock is established. Publish the held descriptor before
                # clearing the pending marker so a fork child always sees at
                # least one discoverable reference. The child hook deduplicates
                # the two fields when a fork lands between these assignments.
                self._fd = fd
                self._owner_pid = os.getpid()
                self._pending_fd = None
            except AuthorityBusyError as exc:
                try:
                    self._rollback_failed_acquisition(fd)
                except AuthorityOwnerError as close_exc:
                    raise close_exc from exc
                raise
            except (OSError, AuthorityOwnerError) as exc:
                # `_open_lock_file` can itself discover an ambiguous close while
                # its local fd has not yet been returned to this frame. In that
                # case keep the pending descriptor + registry reservation intact.
                if not self._release_broken:
                    try:
                        self._rollback_failed_acquisition(fd)
                    except AuthorityOwnerError as close_exc:
                        raise close_exc from exc
                if isinstance(exc, AuthorityOwnerError):
                    raise
                raise AuthorityOwnerError(
                    f"could not acquire authority owner lock {self._lock_path}: {exc!r}"
                ) from exc
            except BaseException as exc:
                # If open entered the kernel/publication interval but no integer
                # became discoverable in either the caller local or owner fields,
                # Python cannot know whether a live descriptor escaped the frame.
                # Fail-stop process death is the only sound cleanup: the OS closes
                # every descriptor and releases any lock before replacement.
                if (
                    self._opening_unpublished
                    and fd is None
                    and self._pending_fd is None
                    and self._fd is None
                ):
                    os._exit(self._HIDDEN_OPEN_EXIT_CODE)

                # Otherwise the descriptor is known and can be closed normally.
                # KeyboardInterrupt/SystemExit during low-level setup must not
                # strand a clean descriptor/registry state. If cleanup itself is
                # ambiguous, the close error supersedes the interruption and the
                # domain remains reserved fail-closed.
                try:
                    self._rollback_failed_acquisition(fd)
                except AuthorityOwnerError as close_exc:
                    raise close_exc from exc
                raise

            return self

    def release(self) -> None:
        """Close the private owner handle without deleting the rendezvous file."""

        with self._state_lock:
            if self._release_broken:
                raise AuthorityStateError(
                    "owner lock release previously failed; terminate the process "
                    "instead of attempting ownership transfer"
                )
            if self._pending_fd is not None or self._opening_unpublished:
                raise AuthorityStateError("owner lock acquisition is still in progress")
            if self._fd is None or self._owner_pid != os.getpid():
                raise AuthorityStateError("owner lock is not held by this process")

            fd = self._fd
            # Couple process-local reservation release to the private-handle
            # close. Verify registry authority before crossing the OS release
            # boundary. If interruption happens after close/unpublication but
            # before registry deletion, the exception handler below completes the
            # descriptor-free registry transition before propagating it.
            with self._registry_guard:
                if self._registry.get(self._identity) is not self:
                    raise AuthorityOwnerError(
                        "authority owner registry lost the active domain reservation"
                    )
                try:
                    self._close_and_unpublish_fd(fd, phase="authority owner handle")
                    del self._registry[self._identity]
                except BaseException:
                    if (
                        not self._release_broken
                        and self._fd is None
                        and self._pending_fd is None
                    ):
                        if self._registry.get(self._identity) is self:
                            del self._registry[self._identity]
                    raise

    def __enter__(self) -> "AuthorityOwnerLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    @classmethod
    def _before_fork(cls) -> None:
        """Block fork across owner-descriptor publish/unpublish transitions.

        CPython reports exceptions from registered at-fork callbacks as
        unraisable and can continue the fork. Therefore a signal-interrupted
        ``Lock.acquire`` cannot be allowed to escape: retry until the fork gate is
        actually held. This may defer an interrupt, but it cannot unlock another
        thread's critical transition or permit an unsafe fork snapshot.
        """

        while True:
            try:
                cls._fork_guard.acquire()
            except BaseException:
                continue
            return

    @classmethod
    def _after_fork_parent(cls) -> None:
        cls._fork_guard.release()

    @classmethod
    def _after_fork_child(cls) -> None:
        """Detach inherited owner descriptors in a POSIX fork child.

        ``flock`` ownership may be associated with an inherited open-file
        description. Calling explicit unlock in the child could therefore drop
        the parent's lock. The child closes only inherited descriptor copies,
        including a descriptor whose acquisition had not yet reached flock,
        clears copied registry state, and gets fresh thread locks. Full fork
        semantics remain a later M7 platform-qualification claim.
        """

        inherited = list(cls._registry.values())
        if any(owner._release_broken for owner in inherited):
            # A broken parent may advertise an fd integer whose close outcome is
            # unknown; that integer may already belong to an unrelated resource.
            # Selective child cleanup is therefore unsafe. Fail-stop the child:
            # process exit closes every inherited descriptor copy without ever
            # letting the child run with copied authority state or stale fd
            # identity. The broken parent is already required to terminate.
            os._exit(cls._BROKEN_FORK_EXIT_CODE)

        # Deduplicate globally, not just per owner: pending/held fields may both
        # name the same live descriptor during a clean promotion.
        inherited_fds = {
            fd
            for owner in inherited
            for fd in (owner._pending_fd, owner._fd)
            if fd is not None
        }
        for fd in inherited_fds:
            cls._close_noexcept(fd)
        for owner in inherited:
            owner._pending_fd = None
            owner._fd = None
            owner._owner_pid = None
            owner._release_broken = False
            owner._opening_unpublished = False
            owner._state_lock = threading.RLock()
        cls._registry = {}
        cls._registry_guard = threading.Lock()
        # The before-fork callback held the inherited copy at fork time. No other
        # thread survives in the child, so replace it with a fresh unlocked gate
        # rather than trying to recover the parent's lock state.
        cls._fork_guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=AuthorityOwnerLock._before_fork,
        after_in_parent=AuthorityOwnerLock._after_fork_parent,
        after_in_child=AuthorityOwnerLock._after_fork_child,
    )
