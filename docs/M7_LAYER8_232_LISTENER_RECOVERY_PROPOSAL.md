# M7 Layer 8 — Proposal: Listener Recovery for ConnectNamedPipe Error 232

**Status: PROPOSAL, REVISED — for separate review. No production code is modified by this document.** Revision 2 incorporates the design review: F04-RP01 (namespace continuity — close-last-handle-then-create DROPPED as the primary; reset-existing evaluated FIRST, create-before-retire SECOND) and F04-RP02 (a linearizable recovery-versus-shutdown lifecycle rule replacing the insufficient `_running`-only guard, with its deterministic barrier test).

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

## 4. Binding invariants

## 4. Binding invariants (any implementation must preserve all six)

1. The authority ownership lock remains held throughout recovery — recovery is transport-local and never touches ownership.
2. The current-user DACL and remote-client rejection (`PIPE_REJECT_REMOTE_CLIENTS` + the single-ACE descriptor) remain enforced on every recreated instance — `_create_instance` already constructs both.
3. No disconnected pipe handle is reused incorrectly — the retire-and-recreate path closes the dead handle before creating its replacement; no handle is shared across recovery cycles.
4. Intentional shutdown cannot trigger listener resurrection — recovery runs only inside the accept loop's own error path while `_running` is set; the existing `close()`/stop sequence keeps precedence, and an instance-creation failure during shutdown surfaces as shutdown, not resurrection.
5. Unexpected instance-creation failure remains visible and fail-closed — if the replacement `_create_instance` fails, the loop breaks exactly as today (availability loss is observable, never silently degraded), with the error logged with its Win32 code.
6. Recovery replays nothing and bypasses nothing — no request, handshake, or identity validation is affected; a recovered listener serves only fresh connections through the unchanged protocol gates.

### 4.5 The linearizable recovery-versus-shutdown rule (F04-RP02)

`_running` alone CANNOT gate recovery: `stop()` sets it False, closes the endpoint, and RELEASES the security-descriptor/ACL backing allocations — a recovery thread that checked `_running` before that sequence could create a new instance against freed security resources or publish a handle after close. The implementation must instead define:

- **Explicit endpoint states**: `ACTIVE` → `RECOVERING` → back to `ACTIVE`, or → `CLOSING` (terminal).
- **A synchronized publication decision**: whether a replacement/reset instance may be published is decided under one lock that the shutdown path ALSO acquires when entering `CLOSING` — publication after `CLOSING` is impossible by construction. The lock covers the recovery decision, handle publication, and close-entry ONLY; it is never held across a blocking `ConnectNamedPipe`.
- **Exact handle ownership**: the listener thread owns the live instance handle; a replacement is owned by its creator until published (then the listener owns it) or until cleaned up (never published → creator closes it exactly once).
- **Cleanup of unpublished work**: if shutdown wins while a recovery is in flight, the unpublished reset/replacement is closed by its owning thread and the endpoint remains `CLOSING` — no resurrection.
- **Security-descriptor lifetime**: the `SECURITY_ATTRIBUTES`/ACL buffers live as long as ANY instance creation or publication can reference them — released only after `CLOSING` is entered under the same lock.

**Deterministic negative test (required):** a barrier immediately before recovery publishes its reset/replacement; shutdown is triggered from another thread AT the barrier; the oracle must prove — recovery reached the barrier → shutdown began → endpoint entered `CLOSING` → barrier released → NO new listening instance published → every handle closed exactly once → no client accepted after shutdown.

**Namespace-continuity negative test (required, F04-RP01):** throughout the ENTIRE recovery sequence — including a deliberately faulted `DisconnectNamedPipe` — a competing-process probe repeatedly attempts to create/open the pipe name; it must NEVER observe a creatable or foreign-owned name. (For create-before-retire, the probe runs while the original handle is held.)

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
| Shutdown raced against recovery at the publication barrier | No instance published after CLOSING; handles closed exactly once; no client accepted after shutdown | The F04-RP02 deterministic barrier test (§4.5) |
| Pipe name vacated or foreign-creatable at ANY point during recovery | Never — namespace continuity holds throughout, including faulted recovery | The F04-RP01 competing-process probe (§4.5) |
| Windows Server 2025, Python 3.11/3.12 | Native qualification passes | The pinned `windows-2025` job |

## 6. Oracle promotion — the critical qualification requirement

Today the F04 diagnostic intentionally RECORDS reproduction rather than failing on it. **Under the repair, the pre-accept arm must become a strict acceptance oracle:** after each pre-accept disconnect, the listener must survive or recover AND a subsequent client must complete an actual IPC exchange (hello + request + response). A green run containing `REPRODUCED_PRE_ACCEPT` must be impossible once the repair lands — the same test inverts from recording the defect to proving the fix. Until then, the diagnostic stays as the defect's witness. No green diagnostic may be presented as repair qualification.

## 7. Explicitly not in this proposal

No client-visible behavior change (connection errors surface unchanged); no change to the 231 condition (the approved retry design addresses that separately); no ownership, DACL, or protocol-gate semantics changes; no retry of anything post-accept.
