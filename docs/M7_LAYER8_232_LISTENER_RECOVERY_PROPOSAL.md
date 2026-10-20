# M7 Layer 8 — Proposal: Listener Recovery for ConnectNamedPipe Error 232

**Status: PROPOSAL — for separate review under the frozen six-part production-change rule. No production code is modified by this document.**

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

## 3. Candidate approaches (evaluation order per the review)

| Approach | Mechanism | Consideration |
|---|---|---|
| **Retire-and-recreate (preferred for evaluation first)** | Close the disconnected instance's handle; create a fresh listening instance via the existing validated `_create_instance(first=False)` path; continue the loop | Fits the current instance-creation architecture; must preserve listener identity and shutdown ordering |
| Reset-existing | `DisconnectNamedPipe` on the instance, then resume accepting on the same handle | Smaller allocation change; requires careful handle-state management and the same identity/shutdown proofs |

## 4. Binding invariants (any implementation must preserve all six)

1. The authority ownership lock remains held throughout recovery — recovery is transport-local and never touches ownership.
2. The current-user DACL and remote-client rejection (`PIPE_REJECT_REMOTE_CLIENTS` + the single-ACE descriptor) remain enforced on every recreated instance — `_create_instance` already constructs both.
3. No disconnected pipe handle is reused incorrectly — the retire-and-recreate path closes the dead handle before creating its replacement; no handle is shared across recovery cycles.
4. Intentional shutdown cannot trigger listener resurrection — recovery runs only inside the accept loop's own error path while `_running` is set; the existing `close()`/stop sequence keeps precedence, and an instance-creation failure during shutdown surfaces as shutdown, not resurrection.
5. Unexpected instance-creation failure remains visible and fail-closed — if the replacement `_create_instance` fails, the loop breaks exactly as today (availability loss is observable, never silently degraded), with the error logged with its Win32 code.
6. Recovery replays nothing and bypasses nothing — no request, handshake, or identity validation is affected; a recovered listener serves only fresh connections through the unchanged protocol gates.

## 5. Qualification plan (the review's ten scenarios)

| Scenario | Required result | Test vehicle |
|---|---|---|
| Client disconnects before `ConnectNamedPipe` | Listener recovers | The pre-accept reproduction arm, promoted to a strict acceptance oracle (§6) |
| Client disconnects after `ConnectNamedPipe` | Existing behavior preserved | The quick-close arm + existing transport tests |
| Repeated pre-accept disconnects | Listener remains available | N consecutive pre-accept rounds, all followed by successful service |
| Fresh client after recovery | Completes a REAL hello and request round trip | A full `IPCClient` exchange after each recovery — not merely a connect |
| Recovery instance creation fails | Explicit fail-closed outcome | Faulted `_create_instance` during recovery; loop exits, error visible |
| Shutdown during recovery | No listener resurrection | Stop requested mid-recovery; no further accepts |
| Foreign-user access after recovery | Remains denied | The M-7 non-owner local-identity negative re-run post-recovery |
| Remote-client access after recovery | Remains rejected | DACL/flag inspection on the recreated instance; T59's remote campaign stays separate |
| Authority successor after recovery | Existing identity rules preserved | The RP-02 successor-transition negative |
| Windows Server 2025, Python 3.11/3.12 | Native qualification passes | The pinned `windows-2025` job |

## 6. Oracle promotion — the critical qualification requirement

Today the F04 diagnostic intentionally RECORDS reproduction rather than failing on it. **Under the repair, the pre-accept arm must become a strict acceptance oracle:** after each pre-accept disconnect, the listener must survive or recover AND a subsequent client must complete an actual IPC exchange (hello + request + response). A green run containing `REPRODUCED_PRE_ACCEPT` must be impossible once the repair lands — the same test inverts from recording the defect to proving the fix. Until then, the diagnostic stays as the defect's witness. No green diagnostic may be presented as repair qualification.

## 7. Explicitly not in this proposal

No client-visible behavior change (connection errors surface unchanged); no change to the 231 condition (the approved retry design addresses that separately); no ownership, DACL, or protocol-gate semantics changes; no retry of anything post-accept.
