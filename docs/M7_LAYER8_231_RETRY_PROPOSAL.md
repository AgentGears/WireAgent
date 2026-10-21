# M7 Layer 8 — Proposal: Bounded Pre-Admission Connection Retry for Named-Pipe Error 231

**Status: PROPOSAL, REVISED — for separate review. No production code is modified by this document.** Revision 2 incorporates the repair-proposal review: RP-01 (the retry carries its OWN independently-enforced deadline), RP-02 (a retry can encounter a SUCCESSOR authority — reopening the same pipe name is not same-instance), RP-03 (the access-denied negative is independently executable on the pinned runner), the direct-measurement correction (the E7b→E7c CreateNamedPipeW interval is ~0.04 ms, NOT 2.6 ms — the earlier figure related client failures to instance events, not the API call's duration), and the TWO-CONDITION framing (this proposal addresses ONLY the demonstrated 231 accept-gap; the M-13-F04 error-232 listener termination is a SEPARATE availability defect with its own investigation and repair gate — a client-side retry cannot repair a dead accept loop). Per the frozen matrix, the 231 repair is its own reviewed delta; this document is that delta's proposal, submitted alongside the causal evidence already accepted (controlled accept-gap demonstration, run `39462123319` at head `9b007d2`).

## 1. Evidence basis (accepted)

- The controlled experiment DEMONSTRATED on Windows Server 2025 (both interpreters): while the production `accept()` is between consuming a connection and creating the successor instance, a concurrent client's `CreateFileW` receives `ERROR_PIPE_BUSY` (231), and connectivity resumes the moment the successor exists.
- Measured native behavior, corrected per the review: the DIRECT `CreateNamedPipeW` execution (E7b→E7c) is ~0.04 ms on the qualified image. The earlier ~2.6 ms figure measured client-failure-to-instance-event spacing, not the API duration. The 25 ms candidate deadline is therefore an EXPERIMENTAL STARTING POINT for native validation — not a value derived from a measured creation latency.
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
2. **Bounded duration (RP-01 corrected).** The retry carries its OWN monotonic deadline, independently enforced — the transport's existing `request_timeout_s` governs the SERVER-side response wait only and does NOT bound client connection establishment. Proposed EXPERIMENTAL starting value: 25 ms, to be validated natively (and adjusted from observed gap distributions) before any production claim. Deadline semantics: individual waits are short; scheduling delays consume the same deadline; exhaustion is an explicit, distinguishable connection failure.
3. **Error specificity.** Only the observed pipe-busy condition (Win32 error 231 surfaced by `CreateFileW`) qualifies. All other errors propagate immediately.
4. **Fail-closed behavior.** Deadline exhaustion, endpoint disappearance (error 2), access denial (error 5), and every other failure remain observable as explicit connection failures.
5. **Lifecycle correctness (RP-02 corrected).** A retry CAN encounter a successor authority: the pipe name derives from the domain, and a successor owner of the same domain recreates it. Reopening the same name does NOT guarantee the same authority instance, and `IPCClient.connect()` does not itself compare the received `authority_instance_id` against a caller-supplied expected identity. The proposal therefore requires: (a) existing protocol/build gates apply unchanged to every retried connection; (b) a DEDICATED negative test in which attempt 1 hits the old owner (231), the old owner exits, a successor binds the same name, and attempt 2 connects — proving the stale-instance/identity gates reject any request minted against the old owner; (c) the retry itself introduces no new identity claim.

## 4. Why client-side before server-side

Overlapping pipe-instance creation (a server-side alternative) modifies the listener lifecycle, concurrency behavior, and handle ownership — a larger change surface with new failure modes of its own. The client-side retry is confined to the pre-admission seam the causal evidence already characterizes, and its failure mode without the fix is a visible connection error, not silent corruption. If the retry proves insufficient in qualification, the server-side design remains available as a follow-up proposal.

## 5. Qualification plan for the repair delta (the review's ten scenarios)

| Scenario | Required result | Test vehicle |
|---|---|---|
| Transient 231 during controlled gap | Connect after successor becomes available | The controlled gap experiment, client under retry |
| Persistent 231 | Fail within the deadline | Barrier held past the deadline |
| Access denied | Immediate failure, no retry | Independently executable on the pinned runner: a controlled `CreateFileW` returning error 5 proves the loop retries ONLY 231 (RP-03 — the genuine remote/DACL test stays in T59's own campaign and is NOT a prerequisite here) |
| Endpoint missing | No indefinite waiting | Missing pipe name |
| Server shutdown during retry | Terminate safely | Endpoint close mid-retry |
| Owner replacement during retry | Existing identity validation preserved | RP-02's dedicated successor-transition negative: 231 on old owner → owner dies → successor binds same name → retry connects → stale-instance gates reject old-owner requests |
| Concurrent clients | Both connect within the contract | The concurrent-clients transport test |
| T58 startup connection | No worker death from a recoverable gap | The T58 raw-frame worker |
| Post-admission connection failure | No operation replay | The disconnect-after-send law tests (unchanged) |
| Native Windows 3.11 / 3.12 | Both qualified | The pinned windows-2025 job |

The controlled gap experiment is the primary regression test: with the retry, client B in the held-gap window connects after the barrier releases, within the deadline, with zero request bytes sent.

## 6. The second condition this proposal does NOT address (M-13-F04)

The F03 server-progress evidence exposed a SEPARATE defect class: the production `_accept_loop` breaks on `RuntimeError` from `accept()`, and a `ConnectNamedPipe` error 232 (ERROR_NO_DATA — a client disconnecting during connection establishment) can therefore TERMINATE the listener mid-traffic while the owning process is still running. A bounded client-side retry cannot repair an accept loop that has stopped creating instances. F04's reproduction instrument is in the diagnostic suite; its investigation, any repair (for example treating 232-with-listener-running as recoverable), and its qualification are a SEPARATE gate under the frozen six-part production-change rule.

## 7. Explicitly not in this proposal

- No server lifecycle change. No `WaitNamedPipeW` commitment (evaluated during implementation only if the simple bounded wait proves insufficient — either variant stays inside the same contract). No retry of anything post-admission. No change to any other error's behavior.
