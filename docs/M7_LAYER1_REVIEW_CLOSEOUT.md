# M7 Layer 1 — Review Close-out Record

This record supplements `docs/STATE.md` for PR #19 without changing the frozen
normative contract in `docs/M7_DESIGN.md`.

## Scope and claim ceiling

M7 Layer 1 implements only the standalone `AuthorityOwnerLock` mechanism and
canonical authority-domain identity. It does not make the owner lock mandatory
around Dispatcher, recovery, browser startup, or IPC. That runtime integration
starts in Layer 2. Full supported POSIX process-mode qualification remains Layer
7; Windows Server cross-process ownership/IPC qualification remains Layer 8.

The Layer-1 fork tests deliberately exercise specific owner-descriptor
publication/unpublication and fail-stop seams. They do **not** establish that
arbitrary multi-threaded `fork()` is generally safe.

## Fresh independent-review findings after candidate c34a0cfc

- **F27 / Codex P1 — interrupted at-fork prepare acquisition.** A raising signal
  can make `_fork_guard.acquire()` ownership ambiguous while CPython may treat an
  at-fork callback exception as unraisable and continue the fork. The final
  mechanism fail-stops the process if prepare acquisition raises or does not
  report success; it never guesses whether the non-reentrant guard was acquired.
- **F28 / Codex P2 — post-close registry cleanup interruption.** An asynchronous
  exception after clean descriptor close/unpublication but before registry
  deletion could strand a descriptor-free strong reservation. Release now
  completes registry removal when descriptor state is already proven empty.
- **F29 / Codex P2 — interrupted process-domain reservation.** Reservation is now
  inside the acquisition rollback transaction, so an interruption immediately
  after insertion cannot permanently strand a descriptor-free busy domain.
- **F30 / Codex P2 — kernel-open-before-Python-publication interruption.** If a
  raw descriptor may have been created but its integer is not safely discoverable
  by Python state, selective cleanup is impossible. The process fail-stops; OS
  process teardown closes the descriptor before any successor can be admitted.

## Maintainer adversarial re-open after F27–F30

- **F31 — retrying interrupted fork-guard acquisition is unsafe.** The first F27
  repair retried `Lock.acquire()`. That can deadlock if the lock was acquired and
  an exception arrived before Python observed the return value. The final repair
  uses fail-stop instead of retry.
- **F32 — branch-level fail-stop proof was insufficient.** The first hidden-open
  test monkeypatched `os._exit`, proving control flow but not process-death
  cleanup. A real subprocess regression now requires the expected fail-stop exit
  code and then proves a fresh process can acquire the same authority domain.
- **F33 — hidden-open state can be copied by a sibling-thread fork before parent
  fail-stop executes.** A fork child that sees copied `_opening_unpublished`
  state now fail-stops before user code instead of attempting selective fd
  cleanup. This closes the child side of the narrow hidden-descriptor window.

## Final candidate evidence

Exact reviewed candidate: `b8aced40a979d967303db3798fec021ad4944b33`.

GitHub Actions CI #523 is green:

- Ubuntu CPython 3.11: 1012 passed, 6 Windows-only skipped; Ruff clean; mypy clean
  across 87 source files.
- Ubuntu CPython 3.12: full gate green; the deliberate multi-threaded-fork
  boundary regression emits CPython's warning that general multi-threaded fork
  may deadlock. This warning is part of the reason the Layer-1 claim is bounded.
- Windows Server 2025 CPython 3.11 and 3.12 focused jobs are green, including the
  fresh-process hidden-open fail-stop/successor probe. Windows evidence remains
  implementation portability/exclusion evidence, not Layer-8 qualification.

No runtime integration, IPC authority, timed lease, stale-owner stealing, or
automatic mutation replay policy is introduced by Layer 1.
