# M7 Layer 8 — Candidate Qualification Test Matrix

**Status: CANDIDATE — not frozen.** This matrix requires its own review before it becomes the frozen execution plan (handoff §9).
**Inputs:** the approved coverage inventory (`docs/M7_LAYER8_INVENTORY.md` at `3abc2bf`), the frozen `docs/M7_DESIGN.md`, and the Layer 8 boundary handoff.
**Rule carried from the inventory:** existing verified coverage and new qualification work are distinguished in every row; no simulated evidence is promoted into a production or native-platform claim.

Classification legend: **EXISTING-VERIFIED** (test exists; evidence cited in the approved inventory) · **EXISTING-TEST/NEW-ENVIRONMENT** (test exists; the qualification gap is execution on the required environment) · **NEW-WORK** (test or infrastructure to build).

---

## A. Environment and evidence infrastructure

| ID | Item | Class | Specification | Acceptance criterion |
|---|---|---|---|---|
| E-1 | Pin the Windows qualified environment | NEW-WORK (workflow) | Select an explicit Windows Server 2025 runner label, or add a runtime gate using a native OS-product identity check (NOT a `platform.platform()` substring — Python 3.11/3.12 report different strings on the same image, per the approved inventory) | A job on any other edition FAILS the gate; the gate's own negative control (assert the check rejects a forged/mismatched identity input) is exercised in CI |
| E-2 | Extend the Windows job selection | NEW-WORK (workflow) | Add to the pinned Windows job: all Windows-active `test_m7_layer7_*` files (ownership, crash matrix, t16 race, supported faults, ipc faults, media boundary, browser child, harness safety) and `test_m7_layer6_ingress.py` (Windows one-handle tests) | Green run on the pinned image with per-file pass/skip counts recorded; the POSIX-only skips appear as skips, never as failures |
| E-3 | Per-run evidence record | NEW-WORK (workflow) | Each native execution records: OS product + build, filesystem, Python, dependency versions, source revision, `runtime_build_id`, test configuration | The record is emitted into the job log/artifact for every qualification run; STATE entries cite it |

## B. Integrated harness on Windows Server 2025 (W8-01/02/04/05/06; inventory L8-R2)

| ID | Requirement (M7 trace) | Class | Test | Environment | Observable evidence | Negative control | Acceptance |
|---|---|---|---|---|---|---|---|
| M-1 | Sibling exclusion, unclean death, takeover-with-hydration, rendezvous persistence under the INTEGRATED owner (T1–T12, T53) | EXISTING-TEST/NEW-ENVIRONMENT | `test_m7_layer7_ownership.py` (full-owner family; primitive sibling races already QUALIFIED-25 per the inventory) | Pinned Server 2025 | Existing process-boundary assertions: exit codes, JSON records, endpoint state, contender verdicts | Existing adversarial controls in-file | Green on pinned image with E-3 record |
| M-2 | Windows lock lifetime under supported faults (T65; RV03 owner-local churn; ENOSPC at the durable seam) | EXISTING-TEST/NEW-ENVIRONMENT | `test_m7_layer7_supported_faults.py` (Windows-active set: owner-local churn, foreign churn, ENOSPC; rendezvous-loss remains QUALIFIED-POSIX and is NOT claimed here) | Pinned Server 2025 | Existing: successor-refused probes, invoke counts, intent-hash-bound ledger rows | Existing: fail-closed helper controls in `test_m7_layer7_harness_safety.py` (E-2 adds them) | Green on pinned image |
| M-3 | Drain/admission races, blocked-work retention, hung-owner kill, shutdown ordering (T16–T18, T42–T48) | EXISTING-TEST/NEW-ENVIRONMENT | `test_m7_layer7_t16_race.py`, crash-matrix files | Pinned Server 2025 | Existing: begin-drain seam markers, invoke-accounting invariants, durable-state assertions | Existing in-file | Green on pinned image |
| M-4 | Request identity: retained table, saturation, canonical hashing, lost responses (T22–T26, T57–T58) | EXISTING-TEST/NEW-ENVIRONMENT | `test_m7_layer7_ipc_faults.py` (Windows-active set) | Pinned Server 2025 | Existing: raw-frame probes + invoke-count oracles | Existing: stale-instance/dup-id negatives | Green on pinned image |
| M-5 | Crash matrix durability (T34–T38) with real OS-death on the qualified image | EXISTING-TEST/NEW-ENVIRONMENT | `test_m7_layer7_crash_matrix.py` | Pinned Server 2025 | Existing: durable rows, torn-append refusal, successor hydration ordering | Existing in-file | Green on pinned image |

## C. T59 — the native security pair (inventory L8-R3)

| ID | Requirement | Class | Test | Environment | Observable evidence | Negative control | Acceptance |
|---|---|---|---|---|---|---|---|
| M-6 | Live DACL inspection: the pipe's ACTUAL security descriptor carries exactly one ACE — the creating user's SID — and nothing else | NEW-WORK | New test: bind the production endpoint, read the live descriptor via `GetSecurityInfo`/`GetSecurityDescriptorDacl`, parse the ACEs | Pinned Server 2025 (and dev Win11) | Parsed ACE list from the LIVE kernel object — construction success is not evidence (inventory L8-R3) | A deliberately permissive pipe (Everyone ACE), created by the test, must FAIL the same readback oracle — proving the oracle detects foreign grants | Readback shows exactly the single user-SID ACE on the production pipe; the permissive-pipe control fails as designed |
| M-7 | Non-owner local identity rejection | NEW-WORK | New test: create a second local user on the runner (GH Windows runners run with admin), run a real client process as that user against the production pipe | Pinned Server 2025 | The non-owner client's connection is DENIED by the DACL, observed as the client's error, while a same-user client connects | REACHABILITY proof first (per the approved review): the same non-owner client CAN connect to a deliberately permissive pipe — otherwise a broken path would masquerade as rejection | Non-owner denied on the production pipe AND accepted on the permissive control pipe |
| M-8 | True-remote rejection (`PIPE_REJECT_REMOTE_CLIENTS`) | NEW-WORK — **environment decision** | A genuinely remote client requires a second host; single-runner CI cannot produce it | Two-host run or explicit report | Either: a recorded two-host native execution, or the explicit §6-style report that remote rejection is kernel-guaranteed by flag + DACL but not remotely exercised in CI | n/a until environment decided | Matrix records the decision; no silent claim |

## D. W8-07 — real browser-binary lifecycle (inventory L8-R4; handoff §6)

| ID | Requirement | Class | Test | Environment | Observable evidence | Negative control | Acceptance |
|---|---|---|---|---|---|---|---|
| M-9 | Environment feasibility for the real SDK on the qualified image | NEW-WORK (decision) | Provisioning proof: Super-Browser SDK + its Chromium/Patchright binary install and launch headless on the pinned runner | Pinned Server 2025 | Recorded launch evidence (browser version reported through the SDK) | n/a (provisioning gate) | Decision recorded: feasible path or explicit limitation report |
| M-10 | Production `SessionManager.start()` with the REAL dependency launches and serves | NEW-WORK | Full-owner scenario with the real SDK (no `_ChildBackedBrowser`), one read capability through the real browser | Pinned Server 2025 | Real browser child processes observed (pid + readiness through the SDK's own reporting); a served request | Attach-gate refusal (existing unit) re-cited; no-adopt invariant via M-11 | Real launch + serve on the qualified image |
| M-11 | Owner crash WITH surviving browser child (T33 native half) | NEW-WORK | Owner dies uncleanly; real browser children survive (pid-identity-verified); successor acquires, starts its OWN browser, never attaches | Pinned Server 2025 | Zombie-rejecting, starttime-tied liveness of the real children across takeover; successor's own instance identity | If the real binary's lifecycle CANNOT leave survivors (sandbox/job ties), the explicit §6 report is REQUIRED and the surrogate T33 (merged, adapter-bounded) remains the ownership evidence — never presented as binary behavior | Survivor case green, OR the explicit limitation report with the surrogate boundary restated |
| M-12 | Browser dies with owner; successor still recovers fresh | NEW-WORK | Kill/observe tree death; successor acquisition + hydration + fresh browser | Pinned Server 2025 | Successor READY + serving with its own runtime | Existing hydration-ordering evidence re-cited (T48) | Green; explicitly NOT substituting for M-11 |

## E. Inherited M5/M6 regression accounting (inventory L8-F04; M7 §23 over M5 §14 / M6 §20)

Method: enumerate the inherited test files; classify each as (a) platform-independent semantics with valid Linux full-suite evidence, or (b) platform-branch-relevant, requiring pinned-Server-2025 execution. Initial file-level pass (production modules with platform branches: `authority.py`, `authority_ipc_endpoint.py`, `authority_media_ingress.py`, `broker.py`, `m5_write_broker.py`, `safety/effect_ledger.py`, `safety/m5_delete_evidence.py`, `safety/reconciliation_ledger.py`, `safety/scoped_authority.py`, `safety/user_rules.py`):

| Inherited surface | Today's evidence | Matrix disposition |
|---|---|---|
| Effect/reconciliation ledger durability, recovery projector, restart, path identity | Server 2025 (CI-selected) | EXISTING-VERIFIED under E-1 pinning |
| Rule-store interprocess fence (`user_rules`) | Server 2025 (CI-selected) | EXISTING-VERIFIED under E-1 pinning |
| Media ingress Windows one-handle tests | Windows 11 dev gate only | E-2 adds them to the pinned job |
| Delete-evidence / scoped-authority / broker platform branches | Linux full suite; branch behavior on Windows not separately exercised | Enumerate the exact branch behavior per module during matrix review; classify each as (a) or (b) — THIS ENUMERATION IS PART OF FREEZING THIS MATRIX |
| All remaining M5 §14 / M6 §20 behavioral regressions | Linux full suite (1458 passed) | Platform-independent evidence accepted where no platform branch exists in the exercised code path — the enumeration above is the authority for "no branch" |

## F. Native 231 investigation (inventory L8-R5; retry remains separate)

| ID | Requirement | Class | Test | Environment | Observable evidence | Negative control | Acceptance |
|---|---|---|---|---|---|---|---|
| M-13 | Establish the OBSERVED mechanism of the intermittent pipe-busy 231 | NEW-WORK (diagnostic) | Instrumented concurrency run on the pinned image: timestamps of `ConnectNamedPipe` completions, next-instance creations, and client `CreateFileW` failures; distinguish the accept-window from other capacity causes | Pinned Server 2025 | A recorded occurrence classified by observed mechanism, or a bounded clean-run record | n/a (diagnostic; produces evidence, not a repair) | The observed-mechanism report lands in STATE; any retry proposal remains a separately reviewed delta confined to demonstrably pre-admission connection failures |

## G. Items deliberately OUTSIDE this matrix

- **F-131** (low; PID-reuse assertion in the fork negative control) — own reviewed delta.
- **Any 231 retry** — separate delta, pre-admission-only, gated on M-13's evidence.
- **No production-source changes** are authorized by this matrix; any proposal during execution follows the handoff §8 six-part evidence rule.
- Exclusions from handoff §11 (replacement substrate, invisible dependencies, failover, distributed authority, etc.) remain excluded.

## Freeze gate

This matrix freezes only after its own maintainer review. Open decisions the review must settle: (1) E-1's mechanism (label pinning vs runtime gate) — GitHub's stable-label availability may decide; (2) M-8's two-host question; (3) M-9's SDK provisioning path on the runner; (4) section E's per-module branch enumeration.
