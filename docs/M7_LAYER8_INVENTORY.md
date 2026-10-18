# M7 Layer 8 — Windows/Native Qualification: Coverage Inventory

**Baseline:** `31d3688` (Layer 7 complete within stated boundaries)
**Status:** Initial maintainer inspection — the first authorized Layer 8 activity per the boundary handoff §12. No production behavior changed.
**Method:** Direct source inspection of the Windows production surfaces (owner lock, named-pipe endpoint/transport, browser-launch path) and the full qualification-test surface, cross-checked against executed CI and local-gate evidence. Every classification below names its evidence; a classification without evidence is a finding, not an assumption.

---

## 1. Environment facts established by this inspection

| Environment | Identity | Evidence |
|---|---|---|
| CI `windows-latest` jobs | **Windows Server 2025** — runner image `windows-2025-vs2026` | Run 37915175948, job `windows durability (3.11)`, "Set up job" log: `Image: windows-2025-vs2026` |
| Local development gate | Windows 11 (build 10.0.26200), real Super-Browser SDK importable, NTFS | Local full-suite runs recorded in STATE (1431 passed + 43 skipped at `c7af8ce`) |
| CI Linux jobs | ubuntu-latest, stub Super-Browser (`tests/stubs/super_browser` via conftest) | Full-suite job, 1458+16 at `c7af8ce` |

**Finding L8-R1 (scope-sharpening):** the CI Windows jobs already execute on the qualified OS *edition*. The handoff's "ordinary Windows CI does not independently prove qualification" resolves precisely: the gap is not the edition but (a) *which suites* the CI Windows job selects (see L8-R2) and (b) the per-run evidence record (§4's fingerprint requirement — the job already prints an OS/python fingerprint; the qualification record must capture it per native execution).

---

## 2. What the CI Windows (Server 2025) job actually executes today

From `.github/workflows/ci.yml` `windows-durability`:

- `test_effect_ledger*.py` (3 files), `test_m6_reconciliation_ledger*.py` (2), `test_m6_reconciliation_path_identity.py`, `test_m6_recovery_projector.py`, `test_m6_layer5_restart.py`, `test_m6_layer6_windows_durability.py` — the durable-ledger and M6 recovery surface.
- `test_m7_authority_owner_lock.py` + `_setup_failures` + `_callback_failures` + `_failstop_processes` — the lock primitive's single-process and fail-stop suites.
- `test_m7_layer4_transport_windows.py` + `test_m7_layer4_ipc.py` — real named-pipe transport and the IPC pipeline.
- `test_user_rules.py` — the rule-store interprocess fence on its msvcrt branch.

**Finding L8-R2 (the central environment gap):** the CI Windows job selects **no Layer 7 multi-process harness scenarios** (`test_m7_layer7_*` is absent). Every process-scale Windows qualification — sibling contention, unclean death and takeover, drain races, crash matrix, supported faults, browser-child takeover — currently runs on Windows **only in the local dev gate, i.e. on Windows 11, not Server 2025**. The harness itself is platform-portable (the full-owner family runs on named pipes there); closing this gap is a CI-selection change plus native execution, not new mechanism code.

**Finding L8-R6:** the M7 media-ingress Windows one-handle tests (`test_m7_layer6_ingress.py:683/703/728`, skipped on Linux) are not selected by the CI Windows job either — Windows coverage for them exists only on the dev box.

---

## 3. Production-surface findings (source inspection)

### 3.1 Named-pipe endpoint (`authority_ipc_endpoint.py`)

The endpoint builds a single-ACE DACL (creating user's SID, `FILE_ALL_ACCESS`) via `SetEntriesInAclW`, sets `PIPE_REJECT_REMOTE_CLIENTS`, and uses `FILE_FLAG_FIRST_PIPE_INSTANCE` on bind for foreign pre-created-pipe detection. The accept loop creates the next instance after each connection.

**Finding L8-R3 (T59's acceptance is not yet established):** `test_windows_endpoint_binds_with_current_user_dacl` asserts only that the construction path *completes* — exactly the "helper returning success" shape the handoff §4 excludes. No test reads back the live pipe's security descriptor (`GetSecurityInfo`/`GetSecurityDescriptorDacl`), and no test drives a genuinely remote client against `PIPE_REJECT_REMOTE_CLIENTS`. Both halves of T59 — "remote client attempts production IPC" and "the ACL is *inspected*" — are untested native cases.

### 3.2 Pipe-busy error 231 (the Layer-4 flake) — admission-phase analysis

Source establishes the mechanism precisely. `WindowsNamedPipeEndpoint.accept()` blocks in `ConnectNamedPipe`, then creates the next instance *after* the current one connects. A second concurrent client whose `CreateFileW` (in `_WindowsPipeConnection.open_client`) lands in the window between those two steps finds zero listening instances and receives `ERROR_PIPE_BUSY` (231). The failure occurs during **connection establishment**: before the hello frame is written, before any request bytes exist, before admission is reachable. Per the handoff §7's classification question: **the error is demonstrably pre-admission by construction**. A future retry confined to `CreateFileW`-phase `ERROR_PIPE_BUSY` in `open_client` would be pre-admission by mechanism; the repair itself remains a separately reviewed delta and must not be folded into qualification work.

### 3.3 Owner lock (`authority.py`, Windows branch)

`msvcrt.locking(LK_NBLCK)` on a dedicated `O_RDWR|O_CREAT` fd opened non-inheritable, with the at-fork gates, registry reservation, and fail-stop ambiguity handling. The known CRT property — byte-range locks are enforced against other processes' access to the locked range — is *observed in evidence* (T65's foreign churn test records OS-refused foreign reads inside the locked byte on Windows 11; POSIX flock advisory asserted symmetric). The same-process unrelated-descriptor hazard (RV03) is qualified by the owner-local churn test — again on Windows 11 only.

### 3.4 Browser-launch reality

The real Super-Browser SDK is importable on the dev box, yet **no test in the repository drives a real browser binary through any launch path** (verified by search). Every browser-carrying scenario uses `_StubSB` (DOM surface) or, in T33, `_ChildBackedBrowser` (dependency-substituted; production `SessionManager.start()` runs against a controlled child). This is by design and recorded in Layer 7's boundary statements — and it means W8-07 is greenfield native territory, not a re-run of existing tests.

---

## 4. Simulation and stub map (what "green" currently means where)

| Surface | Production code exercised | Substituted | Where |
|---|---|---|---|
| Authority chain (lock, session, dispatcher, IPC server/transport, gateway, ledgers, recovery) | **Always production, never stubbed** | — | All suites |
| DOM/broker surface (`_StubSB`) | Kernel admission, confirmation, policy gates | Browser DOM | All m5-stack scenarios |
| Session manager (pre-T33 scenarios) | — | `SessionManager` replaced (stubbed start) | Older Layer-7 scenarios |
| T33 browser dependency | Production `SessionManager.start()`/`stop()`, attach gate, restore | `webwire.session.SuperBrowser` → real OS child | `owner-browser-child` |
| M5 executors | Real executor/runtime/gateway in serving scenarios | Port + evidence reader (controlled adapters, labeled) | `full-owner-serving` and crash matrix |
| Super-Browser SDK | Real on dev box (stub-parity enforced); stub on CI Linux; absent from CI Windows selections | `tests/stubs/super_browser` | Per environment |

---

## 5. Workstream classification against W8-01..W8-08

Legend: **QUALIFIED-25** = real-process evidence on Server 2025 (CI Windows job). **QUALIFIED-11** = real-process evidence on Windows 11 (local gate only). **QUALIFIED-POSIX** = real-process evidence on Linux (transferable mechanism, not native Windows proof). **SIMULATED** = exercised through a controlled substitution. **UNTESTED** = no native case exists.

| Workstream | Classification today | Basis and gaps |
|---|---|---|
| W8-01 Native owner lock | QUALIFIED-25 (single-process lock suites, fail-stop processes) + QUALIFIED-11 (sibling contention, death release, takeover, rendezvous persistence via the L7 harness) | Server-2025 process-scale execution missing (L8-R2) |
| W8-02 Windows lock lifetime | QUALIFIED-11 (owner-local churn, foreign churn, ENOSPC fault, rendezvous loss) + QUALIFIED-25 (primitive suites) | Same environment gap; CRT same-process semantics rest on Win-11 evidence |
| W8-03 Windows IPC security | PARTIAL: transport roundtrips, framing, oversized refusal, build/instance gates QUALIFIED-25; DACL inspection and remote-client rejection UNTESTED (L8-R3); T59 open |
| W8-04 Lifecycle/admission | QUALIFIED-11 (L7 harness: drain races T16, blocked work retention, hung-owner kill, shutdown ordering) | Server-2025 execution missing |
| W8-05 Durable crash recovery | QUALIFIED-11 (crash matrix T34–T38, torn-append, takeover hydration) + QUALIFIED-25 (ledger durability/recovery suites) | Process-scale crash matrix on Server 2025 missing |
| W8-06 Request identity | QUALIFIED-11 (retained table, saturation, canonical identity, lost responses) + QUALIFIED-25 (IPC pipeline suite) | Same environment gap |
| W8-07 Native browser lifecycle | **UNTESTED** (no real browser binary in any test; T33 evidence is within its recorded adapter boundary) | Greenfield; see §6 constraints below |
| W8-08 Regressions/support boundaries | QUALIFIED-25 (M5/M6 ledger/recovery regressions, rules fence) except media-ingress one-handle tests: QUALIFIED-11 only (L8-R6) | Add ingress suite to Server-2025 selection |

---

## 6. Frozen assumptions requiring direct Windows verification

1. **The msvcrt byte-range lock's death-release and non-expiry on Server 2025 under real sibling contention** (currently Win-11 process-scale + Server-2025 single-process).
2. **The named-pipe DACL as-built**: kernel-enforced user-only access and `PIPE_REJECT_REMOTE_CLIENTS` rejection, established by descriptor readback and a real non-local attempt (T59).
3. **The pipe rendezvous-capacity behavior under true concurrency** on the qualified OS (231 classification above; any retry remains pre-admission-only).
4. **Real browser-binary lifecycle** through production `SessionManager.start()` on Windows: startup, owner crash with and without child survival, successor isolation — including the explicit report the handoff §6 requires if the real binary cannot reproduce the surviving-child case (browser-sandbox/job ties may make children die with the launcher; the surrogate then retains the ownership test only, labeled as such).
5. **Job-Object/handle inheritance realities of the real browser tree** for cleanup discipline (the handoff's five-concern separation; no new supervision mechanism is authorized).

---

## 7. Findings register (initial inspection)

- **L8-R1** — CI Windows jobs already run Windows Server 2025 (`windows-2025-vs2026`); the qualification gap is suite selection and evidence recording, not OS edition.
- **L8-R2** — No Layer 7 multi-process scenarios in the CI Windows selection; all process-scale Windows evidence is Windows 11 local-gate evidence.
- **L8-R3** — T59 unestablished: the DACL is never inspected on a live pipe and no remote client is ever driven; the existing test proves construction success only.
- **L8-R4** — No real browser binary in any automated test; W8-07 is greenfield under the §6 constraints (surviving-child case mandatory-adversarial or explicitly reported; surrogate evidence may not be presented as browser-binary behavior).
- **L8-R5** — Pipe-busy 231 classified from source: rendezvous-capacity race in `accept()` between `ConnectNamedPipe` and next-instance creation; the client error arises in `CreateFileW` — pre-admission by construction. Repair is a separate reviewed delta.
- **L8-R6** — M7 media-ingress Windows one-handle tests not in the CI Windows selection (dev-box only).
- **F-131** — unchanged: low-severity PID-reuse assertion in the Layer 7 fork negative control; own delta, no silent incorporation.

---

## 8. Proposed next steps (for maintainer review, not yet executed)

1. Extend the CI Windows job selection to the Layer 7 harness scenarios and the media-ingress suite, with the §4 evidence fingerprint captured per run (tests/workflow only).
2. Build the T59 native pair: live-pipe DACL readback (`GetSecurityInfo` → assert single user-SID ACE) and a real remote-client attempt against `PIPE_REJECT_REMOTE_CLIENTS`.
3. Design the W8-07 browser-binary campaign within §6: real launch through production `start()`, owner-crash both survivor and non-survivor cases, successor isolation, explicit reporting if the binary's lifecycle cannot produce the survivor case.
4. F-131 and any 231 retry as their own reviewed deltas.

None of these steps changes production behavior. Per the handoff, the frozen candidate test matrix is fixed only after this inventory passes maintainer-first review.
