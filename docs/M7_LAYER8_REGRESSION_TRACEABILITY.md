# M7 Layer 8 — Inherited Regression Traceability Register

**Status: CANDIDATE companion to `docs/M7_LAYER8_CANDIDATE_MATRIX.md` §E (L8-MF07). Submitted for review confirmation; its confirmation is part of matrix freeze.**
**Rule:** every numbered requirement (M5 T1–T25, M6 R1–R48) and every additional mandatory regression bullet (M5 §14, M6 §20, M7 §23) appears in exactly one row. Range rows are used only where the named tests genuinely establish the whole range; a reviewer challenging any row splits it.

**Axes:** `OS` = exercises one of the six true OS-branch seams (`authority`, `authority_ipc_endpoint`, `effect_ledger`, `reconciliation_ledger`, `user_rules`, `authority_media_ingress`); `SDK` = depends on Super-Browser behavior beyond type imports (qualified in stub configuration; real-binary axis is the M-9..M-12 campaign); `—` = neither (platform-independent semantics).
**Evidence codes:** `LIN` = Linux full suite (1458 passed at baseline) · `S25` = CI Windows Server 2025 selected suite · `W11` = Windows 11 local gate · `L7W/L7P` = Layer-7 harness, Windows/POSIX lanes.

## 1. M5 T1–T25 (docs/M5_DESIGN.md §14)

| Reqs | Test(s) | Seam | Axis | Evidence | L8 action |
|---|---|---|---|---|---|
| T1 | `test_effect_ledger_durability_retry`, `test_commit_gateway_fault_injection` | effect ledger fsync/retry | OS (nt fsync branch) | LIN + S25 (durability suite) | none beyond E-1 re-run |
| T2, T3 | `test_m5_recovery_guard`, `test_m5_recovery_guard_concurrency`, `test_hydration`; process-scale: L7 crash matrix (T44/T36 scenarios) | recovery guard + ledgers | — (in-proc) / OS (L7 path) | LIN; L7W W11 | M-5 pinned re-run |
| T4 | `test_m5_effect_executor`, `test_m5_first_pass_regressions` | executor runtime | — | LIN | none |
| T5 | `test_m5_actor_bound_post_terminalization`, `test_m5_actor_bound_evidence` | evidence readers | SDK(stub) | LIN | none |
| T6 | `test_scoped_authority_guards`, `test_m5_capability_adapter` | scoped authority | — | LIN | none |
| T7, T10, T11 | `test_m5_target_binding`, `test_confirmation_token_capability_binding`, `test_m5_intent_snapshot_and_mint_time` | permit/intent binding | — | LIN | none |
| T8, T9, T21, T25 | `test_commit_gateway_contract`, `test_commit_gateway_retention`, `test_m5_unconsumed_no_effect_closure`, `test_m5_consume_ttl_linearization` | permit lifecycle | — | LIN | none |
| T12 | `test_journal`, `test_m5_dispatcher_denial_journal` | journal audit-only | — | LIN | none |
| T13 | `test_m5_claim_fence_contract` | claim fence | — | LIN | none |
| T14 | `test_commit_gateway` (+`_codex_final`, `_review`) | gateway reservation | OS (ledger append) | LIN | none |
| T15 | `test_m5_intent_snapshot_and_mint_time` | intent immutability | — | LIN | none |
| T16 | `test_m5_consume_ttl_linearization` | TTL sampling | — | LIN | none |
| T17, T18 | `test_m5_epoch_linearization`, `test_m6_confirmation_state_fail_closed`, `test_commit_gateway_fault_injection` | epoch/expiry fence | — | LIN | none |
| T19 | `test_m5_monotonic_authority_clock` | monotonic clocks | — | LIN | none |
| T20 | `test_m5_epoch_linearization` | epoch fence | — | LIN | none |
| T22 | `test_m5_external_kill_refresh`, `test_kill_switch*` (5 files) | kill switch | — | LIN | none |
| T23, T24 | `test_m5_atomic_grant_spend`, `test_m5_grant_store_pruning` | grant spend | — | LIN | none |

## 2. M5 §14 additional mandatory regressions (37 bullets)

| Bullets | Test(s) | Axis | Evidence | L8 action |
|---|---|---|---|---|
| concurrent grant claims / permit consumers / competing writers (3) | `test_commit_gateway_concurrency`, `test_m5_recovery_guard_concurrency`, `test_scoped_authority_concurrency` | — | LIN | none |
| policy/kill/authority linearization set (5: policy interleave, kill activation, hot-file re-observation, reset listener, re-entrant listeners) | `test_kill_switch`, `test_kill_switch_drain_handoff`, `test_kill_switch_generation_revocation`, `test_kill_switch_reentrancy`, `test_m5_external_kill_refresh`, `test_kernel_risk_gate` | — | LIN | none |
| reset+retrip generations / callbacks outside lock (2) | `test_kill_switch_generation_revocation`, `test_kernel_hygiene` | — | LIN | none |
| epoch bump vs mint/consume, fenced unused expiry, blocked-consume expiry (3) | `test_m5_epoch_linearization`, `test_m5_unconsumed_no_effect_closure`, `test_m5_consume_ttl_linearization` | — | LIN | none |
| outcome APIs canonical attempt / forged correlation keys (2) | `test_commit_gateway_outcomes`, `test_commit_gateway_outcome_lineage`, `test_execution_model_sealing` | — | LIN | none |
| corrupt tail / malformed schema / lineage / transitions fail closed; same-path writer serialization; bytes-written-fsync-failed retry; ambiguous reservation stable effect_id; clean release denied post-I/O; strict terminal evidence; lifecycle field mutation denied (8) | `test_effect_ledger`, `test_effect_ledger_schema`, `test_effect_ledger_durability_retry`, `test_m5_reservation_retry_identity`, `test_m5_write_broker_kill_recheck`, `test_execution_models`, `test_m6_layer6_windows_durability` (Windows fsync/path-identity half) | OS | LIN + S25 | none beyond E-1 re-run |
| final grant validation/spend atomicity, expiry-at-spend, slow-clock TTL, grant-clock-at-reservation (4) | `test_m5_atomic_grant_spend`, `test_m5_grant_store_pruning`, `test_m5_monotonic_authority_clock`, `test_m5_consume_ttl_linearization` | — | LIN | none |
| SAFE_* replay evidence, unproven families UNKNOWN/REQUIRED (2) | `test_effect_policy`, `test_m5_first_pass_regressions` | — | LIN | none |
| intent mutation during blocked reservation; target-missing denial; permit-time sampling; UTC ledger provenance; failed pre-permit cancellation (5) | `test_m5_intent_snapshot_and_mint_time`, `test_m5_claim_fence_contract`, `test_m5_monotonic_authority_clock`, `test_m5_unconsumed_no_effect_closure` | — | LIN | none |
| dispatcher serialization / routing / denial journal / kill interleaving (covered by §23 overlap, listed for completeness) | `test_m5_dispatcher_serialization`, `test_m5_dispatcher_routing`, `test_m5_dispatcher_denial_journal`, `test_m5_dispatcher_kill_interleaving` | — | LIN | none |

## 3. M6 R1–R48 (docs/M6_DESIGN.md §20)

| Reqs | Test(s) | Axis | Evidence | L8 action |
|---|---|---|---|---|
| R1, R2, R6, R26, R27 | `test_m6_recovery_projector`, `test_m6_layer5_restart` | OS (ledger nt branch) | LIN + S25 | none beyond E-1 re-run |
| R3, R4, R5 | `test_m6_reconciliation_coordinator`, `test_m6_reconciliation_evidence_artifacts` | OS | LIN + S25 | none |
| R7, R8, R9, R10, R45 | `test_m6_reconciliation_independent_gates`, `test_m6_reconciliation_dead_live_attempt`, `test_m6_reconciliation_layer4_acceptance` | — | LIN | none |
| R11 | `test_m6_reconciliation_strict_json`, `test_m6_reconciliation_evidence_artifacts` | — | LIN | none |
| R12, R13, R30, R38 | `test_m6_reconciliation_authority`, `test_m6_reconciliation_operator` | — | LIN | none |
| R14, R15, R16, R40 | `test_m6_confirmation_epoch`, `test_m6_confirmation_state_hardening` | — | LIN | none |
| R17, R18, R19, R21, R35, R36, R37 | `test_m6_reconciliation_ledger_durability`, `test_m6_reconciliation_committed_continuation` | OS | LIN + S25 | none beyond E-1 re-run |
| R20, R34 | `test_m6_layer5_restart`, `test_m6_layer5_qualification` | OS | LIN + S25 | none |
| R22, R23, R24, R25 | `test_m6_reconciliation_ledger`, `test_effect_ledger`, `test_journal` | OS | LIN + S25 | none |
| R28 | `test_m6_layer5_sibling_coordinator`, `test_m6_layer5_restart` | — | LIN | none |
| R29 | `test_m6_reconciliation_coordinator` (concurrency) | — | LIN | none |
| R31, R32 | `test_m5_evidence_reader`, `test_m5_leased_read_broker` | SDK(stub) | LIN | none |
| R33, R39 | `test_m6_recovery_publication_fail_closed`, `test_m6_recovery_independent_gates` | — | LIN | none |
| R41 | `test_m6_layer7_replay_safety` | — | LIN | none |
| R42 | `test_kernel_risk_gate` (bucket interplay) | — | LIN | none |
| R43 | `test_kill_switch_drain_handoff` | — | LIN | none |
| R44 | `test_m6_confirmation_state_fail_closed` | — | LIN | none |
| R46 | `test_m6_layer6_windows_durability` | OS | S25 | none beyond E-1 re-run |
| R47, R48 | `test_m6_confirmation_epoch`, `test_m5_consume_ttl_linearization` | — | LIN | none |

## 4. M6 §20 additional mandatory regressions (21 bullets)

| Bullets | Test(s) | Axis | Evidence | L8 action |
|---|---|---|---|---|
| evidence hash determinism/mismatch; strict JSON + identity; forged reserved keys (3) | `test_m6_reconciliation_strict_json`, `test_m6_reconciliation_evidence_artifacts` | — | LIN | none |
| same-path writer serialization; publication cannot regress truth; one effect cannot clear another (3) | `test_m6_reconciliation_ledger`, `test_m6_recovery_projector`, `test_m6_recovery_independent_gates` | OS | LIN + S25 | none beyond E-1 re-run |
| no capability obtains ReconciliationAuthority; inspection read-only; operator ≠ actor identity (3) | `test_m6_reconciliation_dispatcher_composition`, `test_m6_reconciliation_operator`, `test_m6_reconciliation_authority` | — | LIN | none |
| monotonic authority expiry, not persisted; kernel token monotonic liveness; wall-clock diagnostic-only; expiry at synchronized boundary (4) | `test_m6_reconciliation_authority`, `test_m6_confirmation_epoch`, `test_m6_confirmation_state_hardening` | — | LIN | none |
| epoch process-local, no pending tokens on start (1) | `test_m6_confirmation_epoch` | — | LIN | none |
| clean-failure exact-fact re-drive; restart loses committed authority → fresh confirmation (2) | `test_m6_reconciliation_committed_continuation`, `test_m6_layer5_restart` | OS | LIN + S25 | none |
| positive evidence ≠ causal identity; negative evidence ≠ no-effect; missing file valid-empty vs corruption; row without target = corruption; journal absence irrelevant; M5 clocks unaltered (6) | `test_m6_reconciliation_evidence_artifacts`, `test_m6_reconciliation_ledger`, `test_m6_recovery_projector`, `test_journal`, `test_m5_monotonic_authority_clock` | OS (ledger rows) | LIN + S25 | none |

## 5. M7 §23 additional mandatory regressions (13 bullets)

| Bullet | Test(s) | Axis | Evidence | L8 action |
|---|---|---|---|---|
| acquisition failure → no token/safety fact/browser/reconciliation | `test_m7_layer2_runtime_wiring`, `test_m7_layer4_startup_lifecycle` | OS | LIN | M-1 pinned re-run (harness half) |
| stale owner instance cannot mint/consume via IPC | `test_m7_layer5_ipc`, L7 `ipc_faults` (stale-instance negative) | OS | LIN + W11 | M-4 pinned re-run |
| request-cache eviction grants no replay authority | `test_m7_layer5_request_table` | — | LIN | none |
| table pressure cannot evict/orphan admitted work | `test_m7_layer5_request_table` + L7 tablesat | OS | LIN + W11 | M-4 pinned re-run |
| journal audit-only, never owner identity | `test_journal`, `test_m7_layer2_authority_session` | — | LIN | none |
| instance id never changes M5/M6 lineage | `test_m7_layer3_instance_lifecycle`, `test_m7_layer5_reconciliation_routing` | — | LIN | none |
| restart ≠ durable claim from bucket/dedupe reset | `test_m7_layer5_transport`, `test_dispatcher` | — | LIN | none |
| offline reconciliation keeps operator confirmation | `test_m6_reconciliation_operator`, `test_m7_layer5_reconciliation_routing` | — | LIN | none |
| raw diagnostics outside owner never mutation authority | `test_m5_no_legacy_write_route`, `test_m7_layer4_ipc` (allowlist) | — | LIN | none |
| media ingress digest/manifest binding + preflight | `test_m7_layer6_ingress` (incl. the three Windows one-handle tests), `test_m7_layer6_real_stack` | OS (one-handle path) | LIN (POSIX half) + W11 (Windows half) | E-2 adds the Windows tests to the pinned job |
| genuine sibling processes, not two objects | L7 harness family (`ownership`, `crash_matrix`, `t16`, `supported_faults`, `browser_child`, `harness_safety`) | OS | L7P + W11 | M-1..M-5 pinned re-runs |

## 6. Summary for freeze

- Every numbered requirement and bullet above maps to named tests; the `—`/`SDK(stub)` rows carry Linux full-suite evidence under the two-axis justification confirmed in the matrix §E.
- The OS rows reduce to exactly the six seams; their L8 action is E-1-pinned re-execution (plus E-2 for the media-ingress Windows tests) — no new mechanism tests hidden inside the accounting.
- **UNCOVERED items: none identified by this register.** Any requirement a reviewer cannot trace in a row above is, by that fact, a finding against this register — the intended failure mode (L8-MF07's rule: no requirement disappears).
