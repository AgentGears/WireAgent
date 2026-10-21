# M7 Layer 8 — Proposal: Listener Recovery for ConnectNamedPipe Error 232

**Status: PROPOSAL, REVISED — for separate review. No production code is modified by this document.** Revision 4 incorporates design-review round 3: F04-RP06 (the namespace probe split into three controls — first-instance-flag proves NAME continuity, not exclusive server-instance ownership, since Windows permits sibling instances to callers holding FILE_CREATE_PIPE_INSTANCE access, which the production DACL grants the full user); F04-RP07 (namespace continuity decoupled from concurrent client availability — transient 231 during the reset interval is legitimate and stays on its separate repair track); F04-RP08 (a synchronized POST-ACCEPT ADMISSION check before handler dispatch — no returned connection enters the request handler after the terminal shutdown decision — and the reconciliation of 4.5's lock-scope wording with the stronger invariant). Prior revisions' corrections (design-review rounds 1-2) F04-RP03 (namespace continuity scoped to ACTIVE/RECOVERING — the terminal-failure path demonstrates WITHDRAWAL, never an impossible post-teardown name reservation; the competing-server probe uses CreateNamedPipeW with FILE_FLAG_FIRST_PIPE_INSTANCE plus a positive control); F04-RP04 (the lifecycle lock is held across the SHORT kernel-visible operations themselves — reset, creation, close-entry — because a new instance becomes KERNEL-visible at CreateNamedPipeW success, before Python publication; resource lifetime therefore ends only when no in-flight kernel operation remains); F04-RP05 (one authoritative invariant set replaces the contradictory revision-2 carryovers).

## 1. The demonstrated defect (accepted evidence)

The deterministic pre-accept experiment (`test_M13_F04_client_disconnect_listener_termination`, run 39470854173 at `ba4bd8c`) reproduces on Windows Server 2025, both interpreters, 10/10 rounds:

1. The endpoint creates a listening pipe instance (`bind()`).
2. A client connects successfully and closes immediately — BEFORE the server calls `accept()`.
3. The server's `ConnectNamedPipe` on that already-disconnected instance returns **error 232 (ERROR_NO_DATA)** — matching Microsoft's documented lifecycle: a previously connected client closed its handle and the server has not disconnected that instance.
4. `WindowsNamedPipeEndpoint.accept()` raises `RuntimeError` (only `ERROR_PIPE_CONNECTED` is treated as nonfatal).
5. `AuthorityIPCTransport._accept_loop()` breaks on the exception.
6. The listener is dead while the owner process lives; subsequent clients are refused (verified BEFORE any teardown, so the unavailability is the death's consequence).

Sustained stress traffic independently strikes the same window (the accepted CI evidence: 199 accepts then a 232 death mid-run). **An ordinary client disconnect can permanently disable the authority's IPC listener.** This repair takes priority over the 231 retry: losing the listener indefinitely is more consequential than a transient connection failure.

## 2. Why a simple catch-and-continue is NOT the repair

Microsoft documents that a previously connected pipe instance must be disconnected before it can be reused for another client. On error 232 the instance is in a disconnected-client state; merely looping back to `ConnectNamedPipe` on the same handle is not a supported reuse. The repair must explicitly RESTORE A VALID LISTENING INSTANCE.

## 3. Candidate approaches (evaluation order REVISED per F04-RP01)

The original revision preferred close-last-handle-then-create. The review identified the flaw: the ownership lock does NOT reserve the Windows named-pipe NAMESPACE — closing the final server handle can vacate the pipe NAME, and a competing process could create a pipe under that name during the interval before replacement. That ordering is **DROPPED as the primary candidate** and may be reconsidered only with an explicit namespace-continuity argument plus a native falsification test proving the vacancy cannot be exploited.

| Order | Approach | Mechanism | Why |
|---|---|---|---|
| **1st** | **Reset existing instance** | `DisconnectNamedPipe` on the disconnected instance, returning THE SAME pipe object and handle to a valid listening state | Preserves the original handle, pipe object, and security attributes — no namespace vacancy is ever created (Microsoft documents this exact lifecycle for instance reuse) |
| 2nd | **Create-before-retire** | Create the replacement instance WHILE the original handle remains valid; verify; publish under lifecycle control; only then retire the dead instance; continue accepting | Never drops the final reference before replacement exists — but feasibility with the current instance attributes must be established natively first |

The preferred decision flow (candidate 1):

```text
ConnectNamedPipe
    +-- connected            existing path unchanged
    +-- ERROR_NO_DATA (232)
    |       +-- endpoint ACTIVE (per the §4.5 lifecycle rule)?
    |       |       +-- no  -> exit for shutdown
    |       |       +-- yes -> DisconnectNamedPipe
    |       |                   +-- success -> instance returns to listening; loop continues
    |       |                   +-- failure -> FAIL CLOSED (Win32 code preserved; endpoint
    |       |                                  enters an explicitly unusable state)
    +-- any other error      existing failure path
```

## 4. Binding invariants (rev 3 — the single authoritative set)

1. **Ownership is untouched.** The authority ownership lock remains held throughout recovery; recovery is transport-local and never modifies ownership state.
2. **Security attributes persist on every listening instance.** Reset-existing preserves the ORIGINAL instance's DACL and `PIPE_REJECT_REMOTE_CLIENTS` by construction (same pipe object). Any created replacement (fallback) constructs both through the existing `_create_instance` path.
3. **Name continuity, correctly scoped and correctly claimed (F04-RP03/RP06).** While the endpoint is `ACTIVE` or in a recoverable `RECOVERING` state, the server-side pipe OBJECT must remain anchored under the endpoint's own handle: a `CreateNamedPipeW` probe carrying `FILE_FLAG_FIRST_PIPE_INSTANCE` must be refused. This proves NAME CONTINUITY — the original pipe object persists through recovery. It does NOT prove exclusive server-instance ownership: Windows permits ADDITIONAL instances of an existing pipe to callers holding `FILE_CREATE_PIPE_INSTANCE` access with compatible attributes, and the production DACL grants the creating user `FILE_ALL_ACCESS` with `PIPE_UNLIMITED_INSTANCES` — so a same-user process creating a sibling instance WITHOUT the flag is possible BASELINE behavior, predating the F04 repair, and is characterized (not assumed away) by the second control below. TERMINAL failure is out of this scope by design: a failed recovery legitimately closes the last handle; that path demonstrates SAFE WITHDRAWAL. TERMINAL failure is out of this scope by design: a failed recovery legitimately closes the last handle, after which the name may cease to exist; that path instead demonstrates SAFE WITHDRAWAL (the endpoint enters an explicitly unusable state; any later successor binding of the same domain is governed by the unchanged ownership and stale-instance gates).
4. **CLOSING is terminal, and kernel operations are serialized with it (F04-RP04).** The lifecycle lock is held ACROSS the short kernel-visible operations — `DisconnectNamedPipe`, any `CreateNamedPipeW`, and close-entry — and is NEVER held across a blocking `ConnectNamedPipe`. This is stronger than publication-only synchronization, which the review showed insufficient: a new instance becomes KERNEL-visible the moment `CreateNamedPipeW` succeeds, before any Python-level publication. Consequences: shutdown cannot invalidate a handle being reset, cannot free security allocations an in-flight kernel call still uses, and cannot interleave with the creation of a new externally accessible instance. The security-descriptor/ACL buffers are released only after `CLOSING` is entered AND no in-flight kernel operation remains — guaranteed by the lock covering the operations themselves.
5. **Recovery failure is explicit.** A failed `DisconnectNamedPipe` (or failed creation in the fallback) records the original Win32 code, enters a terminal failure state, and fails closed: no silent service, every handle closed exactly once.
6. **Nothing is replayed.** Recovery is transport-local; no request, handshake, or identity validation is replayed or bypassed, and the protocol gates are unchanged.

### 4.5 The linearizable recovery-versus-shutdown rule (F04-RP02)

`_running` alone CANNOT gate recovery: `stop()` sets it False, closes the endpoint, and RELEASES the security-descriptor/ACL backing allocations — a recovery thread that checked `_running` before that sequence could create a new instance against freed security resources or publish a handle after close. The implementation must instead define:

- **Explicit endpoint states**: `ACTIVE` → `RECOVERING` → back to `ACTIVE`, or → `CLOSING` (terminal).
- **A synchronized kernel-operation decision (RP08-reconciled)**: whether a reset or creation may run is decided under one lock that the shutdown path ALSO acquires when entering `CLOSING` — and the lock is held ACROSS the short kernel-visible operations themselves (`DisconnectNamedPipe`, `CreateNamedPipeW`, handle closure, security-resource release), per invariant 4. It is never held across a blocking `ConnectNamedPipe`. Actual handle closure and SD/ACL release are therefore synchronized WITH recovery, not merely sequenced after the `CLOSING` transition.
- **Exact handle ownership**: the listener thread owns the live instance handle; a replacement is owned by its creator until published (then the listener owns it) or until cleaned up (never published → creator closes it exactly once).
- **Cleanup of unpublished work**: if shutdown wins while a recovery is in flight, the unpublished reset/replacement is closed by its owning thread and the endpoint remains `CLOSING` — no resurrection.
- **Post-accept admission check (RP08)**: kernel connection establishment and APPLICATION-level admission are distinct boundaries. An in-flight `ConnectNamedPipe` may legitimately complete around shutdown; the required property is that NO newly returned connection is admitted for processing after the terminal shutdown decision. Before the listener launches a handler for a returned connection, it performs a synchronized admission check; if shutdown has won, the connection is CLOSED WITHOUT beginning the hello/request protocol.
- **Security-descriptor lifetime (F04-RP04)**: the `SECURITY_ATTRIBUTES`/ACL buffers live as long as ANY kernel operation can reference them. Because the lifecycle lock is held across `DisconnectNamedPipe`/`CreateNamedPipeW`/close-entry themselves, entering `CLOSING` implies no in-flight kernel operation remains — only then are the buffers released. (Publication-only synchronization is insufficient: a created instance is kernel-visible before publication.)

**Deterministic negative test (required, RP04/RP08):** TWO interleavings. (a) The reset/create barrier: the barrier sits BEFORE the recovery thread acquires permission to reset or create, shutdown is triggered AT the barrier, and the oracle must prove — recovery reached the barrier → shutdown began → `CLOSING` entered → the recovery thread observed `CLOSING` under the lock and performed NO reset, NO creation, NO kernel-visible change → every handle closed exactly once. (b) The post-accept admission barrier: `accept()` returns a connection, shutdown wins the synchronized decision BEFORE the handler is launched, and the oracle must prove that connection was closed WITHOUT any hello/request byte exchanged and no handler thread started for it.

**Namespace probe, three controls (required, RP06/RP07-corrected):** scoped to the `ACTIVE` and recoverable-`RECOVERING` states.
1. **Name continuity (MANDATORY for F04):** a competing process's `CreateNamedPipeW` WITH `FILE_FLAG_FIRST_PIPE_INSTANCE` on the endpoint's name must always be refused — the original pipe object persists.
2. **Sibling-instance characterization (baseline, not an F04 claim):** the same-user process attempts `CreateNamedPipeW` WITHOUT the flag. The outcome is RECORDED as observed baseline behavior (the documented Windows contract permits it under FILE_CREATE_PIPE_INSTANCE access, which the production DACL grants the user; PIPE_UNLIMITED_INSTANCES=255). If it succeeds, that is an independently scoped security finding predating this repair — never automatically an F04 regression.
3. **DACL boundary validation:** a FOREIGN-SID process attempts `CreateNamedPipeW` without the flag and must be refused by the current-user DACL.
All three carry the positive control (a DIFFERENT unoccupied name creates successfully). Client concurrent availability is deliberately NOT asserted (RP07): transient `ERROR_PIPE_BUSY` during the reset interval is legitimate, stays visible, and belongs to the separate 231 repair track.

## 5. Qualification plan (the review's ten scenarios)

| Scenario | Required result | Test vehicle |
|---|---|---|
| Client disconnects before `ConnectNamedPipe` | Listener recovers | The pre-accept reproduction arm, promoted to a strict acceptance oracle (§6) |
| Client disconnects after `ConnectNamedPipe` | Existing behavior preserved | The quick-close arm + existing transport tests |
| Repeated pre-accept disconnects | Listener remains available | N consecutive pre-accept rounds, all followed by successful service |
| Fresh client after recovery | Completes a REAL hello and request round trip | A full `IPCClient` exchange after each recovery — not merely a connect |
| Recovery instance reset/creation fails | Explicit fail-closed outcome: the original Win32 error preserved, the endpoint left in an explicitly unusable state, and every handle closed exactly once | Faulted `DisconnectNamedPipe` and faulted `_create_instance` during recovery |
| Shutdown during recovery | No listener resurrection | Stop requested mid-recovery; no further accepts |
| Foreign-user access after recovery | Remains denied | The M-7 non-owner local-identity negative re-run post-recovery |
| Remote-client access after recovery | Remains rejected | DACL/flag inspection on the reset/replacement instance — STRUCTURAL evidence only; it does NOT close T59 or M-8, whose genuine remote-host campaigns remain separate blocked requirements |
| Authority successor after recovery | Existing identity rules preserved | The RP-02 successor-transition negative |
| Shutdown raced against recovery BEFORE the reset/create permission | No reset, no creation, no kernel-visible change after CLOSING; handles closed exactly once | The RP04 barrier interleaving (4.5) |
| Connection returned by accept() as shutdown wins | Closed WITHOUT any hello/request byte; no handler dispatched | The RP08 post-accept admission barrier (4.5) |
| First-instance claim on the pipe name during ACTIVE/RECOVERING | Refused — the pipe object persists through recovery | Probe control 1 (RP06) |
| Same-user sibling instance without the flag | RECORDED as observed baseline (permitted by the documented Windows contract under the production DACL; an independently scoped finding if it succeeds — never an automatic F04 regression) | Probe control 2 (RP06) |
| Foreign-SID server-instance creation | Refused by the current-user DACL | Probe control 3 (RP06) |
| Terminal recovery failure | Safe withdrawal: explicit unavailable state, Win32 code recorded, handles closed exactly once — no name-reservation claim beyond teardown | Faulted `DisconnectNamedPipe` rounds in the same probe test |
| Windows Server 2025, Python 3.11/3.12 | Native qualification passes | The pinned `windows-2025` job |

## 6. Oracle promotion — the critical qualification requirement

Today the F04 diagnostic intentionally RECORDS reproduction rather than failing on it. **Under the repair, the pre-accept arm must become a strict acceptance oracle:** after each pre-accept disconnect, the listener must survive or recover AND a subsequent client must complete an actual IPC exchange (hello + request + response). A green run containing `REPRODUCED_PRE_ACCEPT` must be impossible once the repair lands — the same test inverts from recording the defect to proving the fix. Until then, the diagnostic stays as the defect's witness. No green diagnostic may be presented as repair qualification.

## 7. The normative recovery protocol (review-adopted)

```text
WindowsNamedPipeEndpoint.accept()
        |
        v
  ConnectNamedPipe                     [lifecycle lock NOT held]
        |
        +-- SUCCESS / ERROR_PIPE_CONNECTED -> existing path unchanged
        |
        +-- ERROR_NO_DATA (232)
        |       |
        |       v
        |   Acquire lifecycle lock
        |       +-- CLOSING                 -> exit for shutdown
        |       +-- recovery permitted      -> DisconnectNamedPipe   [lock held]
        |               +-- success         -> restore ACTIVE; release lock; continue loop
        |               +-- failure         -> record Win32 code; terminal failure state;
        |                                     fail closed
        +-- any other error                 -> existing failure path
```

**Qualification state table:** ACTIVE = normal secure IPC; RECOVERING = controlled reset with valid ownership and handle lifetime; ACTIVE after recovery = a fresh client completes a real hello/request/response exchange; CLOSING = no new recovery or reactivation; terminal recovery failure = explicit unavailable state, no silent service; successor authority = existing identity and ownership checks preserved. Under the repair, the F04 diagnostic's oracle INVERTS: any REPRODUCED_PRE_ACCEPT outcome FAILS the repair qualification.

## 8. Explicitly not in this proposal

No client-visible behavior change (connection errors surface unchanged); no change to the 231 condition (the approved retry design addresses that separately); no ownership, DACL, or protocol-gate semantics changes; no retry of anything post-accept.
