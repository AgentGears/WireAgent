# M7 Layer 8 — Proposal: Bounded Pre-Admission Connection Retry for Named-Pipe Error 231

**Status: PROPOSAL — for separate review. No production code is modified by this document.** Per the frozen matrix, the 231 repair is its own reviewed delta; this document is that delta's proposal, submitted alongside the causal evidence already accepted (controlled accept-gap demonstration, run `39462123319` at head `9b007d2`).

## 1. Evidence basis (accepted)

- The controlled experiment DEMONSTRATED on Windows Server 2025 (both interpreters): while the production `accept()` is between consuming a connection and creating the successor instance, a concurrent client's `CreateFileW` receives `ERROR_PIPE_BUSY` (231), and connectivity resumes the moment the successor exists.
- Measured native behavior: successor-instance creation completes in ~2.5–2.7 ms on the qualified image. The proposed bounded wait is derived from this measurement (see §3), not chosen arbitrarily.
- Naturally occurring failures (the concurrent-clients transport test; T58's worker startup connect) are consistent with the same pre-admission window; the F03 server-progress accounting now separates prolonged unavailability (server-thread death, e.g. error 232 at shutdown) from independent short gaps.

## 2. The proposed mechanism (client-side, connection establishment only)

```text
Client requests connection
        |
        v
    CreateFileW
        |
        +-- success --> existing hello/admission protocol (unchanged)
        |
        +-- ERROR_PIPE_BUSY (231)
        |       +-- before monotonic deadline? --> bounded wait --> retry CreateFileW
        |       +-- deadline exhausted --> explicit connection failure
        |
        +-- any other error --> existing failure behavior (unchanged)
```

## 3. Contract properties (binding on the implementation)

1. **Pre-admission only.** The retry loop touches ONLY connection establishment. No hello frame, request envelope, or operation invocation may have occurred when a retry fires; nothing is replayed, ever.
2. **Bounded duration.** One monotonic deadline governs the whole connection-establishment attempt. Proposed initial value: **25 ms** (≈10× the measured 2.6 ms successor-creation latency; the transport's existing request timeout remains the outer bound). The value and its measurement basis are recorded in the qualification evidence.
3. **Error specificity.** Only the observed pipe-busy condition (Win32 error 231 surfaced by `CreateFileW`) qualifies. All other errors propagate immediately.
4. **Fail-closed behavior.** Deadline exhaustion, endpoint disappearance (error 2), access denial (error 5), and every other failure remain observable as explicit connection failures.
5. **Lifecycle correctness.** Shutdown, owner death, successor takeover, and stale endpoint identity are unaffected: the retry never crosses an authority boundary — it can only re-attempt opening the SAME pipe name; identity validation happens afterward in the unchanged hello/instance-id gates.

## 4. Why client-side before server-side

Overlapping pipe-instance creation (a server-side alternative) modifies the listener lifecycle, concurrency behavior, and handle ownership — a larger change surface with new failure modes of its own. The client-side retry is confined to the pre-admission seam the causal evidence already characterizes, and its failure mode without the fix is a visible connection error, not silent corruption. If the retry proves insufficient in qualification, the server-side design remains available as a follow-up proposal.

## 5. Qualification plan for the repair delta (the review's ten scenarios)

| Scenario | Required result | Test vehicle |
|---|---|---|
| Transient 231 during controlled gap | Connect after successor becomes available | The controlled gap experiment, client under retry |
| Persistent 231 | Fail within the deadline | Barrier held past the deadline |
| Access denied | Immediate failure, no retry | Permissive-DACL negative from the T59 pair |
| Endpoint missing | No indefinite waiting | Missing pipe name |
| Server shutdown during retry | Terminate safely | Endpoint close mid-retry |
| Owner replacement during retry | Existing identity validation preserved | The stale-instance gates (unchanged) re-run |
| Concurrent clients | Both connect within the contract | The concurrent-clients transport test |
| T58 startup connection | No worker death from a recoverable gap | The T58 raw-frame worker |
| Post-admission connection failure | No operation replay | The disconnect-after-send law tests (unchanged) |
| Native Windows 3.11 / 3.12 | Both qualified | The pinned windows-2025 job |

The controlled gap experiment is the primary regression test: with the retry, client B in the held-gap window connects after the barrier releases, within the deadline, with zero request bytes sent.

## 6. Explicitly not in this proposal

- No server lifecycle change. No `WaitNamedPipeW` commitment (evaluated during implementation only if the simple bounded wait proves insufficient — either variant stays inside the same contract). No retry of anything post-admission. No change to any other error's behavior.
