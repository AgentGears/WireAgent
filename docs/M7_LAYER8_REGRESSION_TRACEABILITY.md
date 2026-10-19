# M7 Layer 8 — Inherited Regression Traceability Register (v3)

**Status: CANDIDATE companion to `docs/M7_LAYER8_CANDIDATE_MATRIX.md` §E. Revision 3 per review findings L8-MF12/MF13/MF14/MF15: the frozen M6 A16–A22 sequence restored exactly, the M5 T23/A15/A29 misattributions corrected to tests that construct the actual prohibited condition, the M6 R3–R5/R6/R7/R10/R22/R23 and M6-A1/A3/A5 mappings corrected to assertion-relevant functions, and the verification rule below adopted as the register's standing standard. Submitted for review confirmation; confirmation is part of matrix freeze.**

**Counting authority (L8-MF09):** M5 §14 additional mandatory regressions = **33 bullets** (IDs M5-A1..A33); M6 §20 = **22 bullets** (M6-A1..A22, where **M6-A22** is the final preservation bullet "M6 does not weaken M5 kill/epoch/policy/permit validation"); M7 §23 = **13 bullets** (M7-A1..A13, where **M7-A1 and M7-A2 are explicit cross-references** to the M5 and M6 obligations mapped in sections 1–4 below, not separately re-mapped). Every numbered requirement (M5 T1–T25, M6 R1–R48) and every bullet appears in exactly one row; a range row is used only where the cited functions genuinely establish the whole range.

**Evidence grades (L8-MF11):** `S25` = THE CITED TEST FILE is in the CI Windows (Server 2025) job selection; `LIN` = Linux full suite only; `W11` = Windows 11 local gate; `L7P/L7W` = Layer-7 harness POSIX/Windows lanes. A row's grade attaches to its cited tests — native qualification is never inferred from a different test on the same platform.

## 1. M5 T1–T25 (docs/M5_DESIGN.md §14) — function-level

| Reqs | Establishing test function(s) | Axis | Evidence |
|---|---|---|---|
| T1 | `test_commit_gateway.py::test_t1_reservation_failure_mints_no_permit_and_does_not_spend`; `test_commit_gateway_fault_injection.py::test_t1_real_ledger_fsync_failure_mints_no_authority`; `test_effect_ledger.py::test_append_durable_fsync_failure_raises_fail_closed` | OS (ledger fsync) | LIN (ledger file also S25) |
| T2 | `test_commit_gateway.py::test_t2_crash_after_reservation_projects_unknown_on_restart`; `test_m5_recovery_guard.py::test_reserved_and_unknown_block_exact_semantic_key` | — | LIN |
| T3 | `test_commit_gateway.py::test_t3_crash_after_submit_leaves_reservation_unresolved` | — | LIN |
| T4 | `test_commit_gateway.py::test_t4_timeout_records_effect_unknown_and_blocks_reuse` | — | LIN |
| T5 | `test_commit_gateway.py::test_t5_observed_effect_stays_recorded_if_later_verification_fails` | — | LIN |
| T6 | `test_scoped_authority_guards.py::test_policy_drift_after_scope_blocks_before_browser_mutation`; `::test_kill_trip_then_reset_blocks_before_permit_mint` | — | LIN |
| T7 | `test_commit_gateway.py::test_t7_target_mismatch_rejected_without_consuming_permit` | — | LIN |
| T8 | `test_commit_gateway.py::test_t8_permit_reuse_rejected` | — | LIN |
| T9 | `test_commit_gateway.py::test_t9_expired_permit_rejected_without_consuming`; `test_m5_first_pass_regressions.py::test_expired_fenced_permit_terminalizes_canonical_attempt` | — | LIN |
| T10, T11 | `test_commit_gateway.py::test_t10_actor_change_rejected`; `::test_t11_intent_change_rejected` | — | LIN |
| T12 | `test_journal.py::test_journal_append_real_io_failure_does_not_raise`; `test_m5_dispatcher_denial_journal.py::test_missing_actor_denial_journals_once_as_denied` | — | LIN |
| T13 | `test_execution_models.py::test_T13_clean_precommit_failure_preserves_approval`; `test_m5_execution_runtime.py::test_clean_precommit_retry_budget_is_bounded` (L8-MF10 correction: the claim-fence file holds only `test_authorize_without_claim_is_gateway_denial_not_model_error`, which does not establish T13's full semantics) | — | LIN |
| T14 | `test_execution_models.py::test_T14_reservation_consumes_approval` | — | LIN |
| T15 | `test_m5_intent_snapshot_and_mint_time.py::test_authorize_uses_one_snapshot_if_caller_mutates_intent_during_fsync` | — | LIN |
| T16 | `test_m5_intent_snapshot_and_mint_time.py::test_permit_ttl_starts_when_permit_is_actually_minted` (L8-MF10 correction) | — | LIN |
| T17 | `test_m5_intent_snapshot_and_mint_time.py::test_grant_expiry_during_required_reservation_closes_without_minting` (L8-MF10 correction) | — | LIN |
| T18 | `test_m5_intent_snapshot_and_mint_time.py::test_failed_prepermit_close_leaves_reservation_unresolved_and_no_authority` (L8-MF10 correction) | — | LIN |
| T19 | `test_m5_monotonic_authority_clock.py::test_default_grant_authority_clock_is_monotonic`; `::test_default_permit_authority_clock_is_monotonic`; `::test_effect_ledger_keeps_wall_clock_utc_timestamp` | — | LIN |
| T20 | `test_m5_epoch_linearization.py::test_direct_epoch_bump_linearizes_after_final_permit_mint`; `::test_direct_epoch_bump_cannot_cross_permit_consumption` | — | LIN |
| T21 | `test_m5_consume_ttl_linearization.py::test_permit_expiring_while_policy_fence_blocks_is_not_consumed` | — | LIN |
| T22 | `test_m5_external_kill_refresh.py::test_hot_file_created_during_required_reservation_blocks_permit_mint`; `::test_hot_file_created_during_consume_wait_blocks_permit_consumption` | — | LIN |
| T23 | `test_m5_target_binding.py::test_best_effort_empty_target_is_denied_before_authority` — constructs an EMPTY target and asserts the `target_missing` denial on the BEST_EFFORT path (L8-MF13 correction: the claim-fence function rejects a VALID-target intent for `claim_not_held` and cannot establish T23); the shared broker-seam denial (`test_m5_write_broker.py`, `target_missing` returns at the binding layer) covers the REQUIRED-path entry | — | LIN |
| T24 | `test_m5_atomic_grant_spend.py::test_gateway_rejects_grant_expiring_at_atomic_spend`; `test_m5_first_pass_regressions.py::test_gateway_holds_grant_claim_fence_through_reservation_and_spend` | — | LIN |
| T25 | `test_m5_atomic_grant_spend.py::test_slow_grant_spend_does_not_consume_new_permit_ttl`; `test_m5_grant_store_pruning.py::test_next_mint_prunes_elapsed_active_grants` | — | LIN |

## 2. M5 §14 additional mandatory regressions — M5-A1..A33, function-level

| ID(s) | Bullet (abbreviated) | Establishing test function(s) | Evidence |
|---|---|---|---|
| A1 | concurrent grant claims → one owner | `test_commit_gateway_concurrency.py::test_concurrent_authorize_same_attempt_creates_one_reservation`; `test_m5_first_pass_regressions.py::test_concurrent_claims_have_exactly_one_owner` | LIN |
| A2 | concurrent permit consumers → one consumption | `test_commit_gateway_concurrency.py::test_concurrent_consumers_allow_exactly_one_boundary_crossing`; `test_m6_confirmation_epoch.py::test_atomic_consume_allows_exactly_one_concurrent_winner` | LIN |
| A3 | competing terminal writers → one fact | `test_commit_gateway_concurrency.py::test_concurrent_terminal_outcomes_record_only_one_terminal_fact` | LIN |
| A4 | live policy registration cannot interleave in final consumption | `test_m5_consume_ttl_linearization.py::test_permit_expiring_while_policy_fence_blocks_is_not_consumed`; `test_scoped_authority_guards.py::test_scoped_and_gateway_must_share_exact_policy_registry` | LIN |
| A5 | kill activation vs authority crossing: one linearization | `test_m5_epoch_linearization.py::test_direct_epoch_bump_cannot_cross_permit_consumption`; `test_scoped_authority_guards.py::test_kill_trip_then_reset_blocks_before_permit_mint` | LIN |
| A6 | hot file re-observed at final boundary | `test_m5_external_kill_refresh.py` (both functions, §T22 row) | LIN |
| A7 | reset listener cannot suppress critical revocation | `test_kill_switch_generation_revocation.py::test_resetting_earlier_listener_cannot_suppress_gateway_epoch_bump`; `test_kill_switch_reentrancy.py::test_listener_reset_does_not_suppress_remaining_trip_delivery` | LIN |
| A8 | re-entrant listeners do not recursively duplicate | `test_kill_switch_reentrancy.py::test_listener_can_call_tripped_without_recursive_reinvocation`; `::test_listener_can_add_listener_during_notification` | LIN |
| A9 | reset+retrip distinct generations | `test_kill_switch_reentrancy.py::test_listener_reset_then_retrip_is_notified_for_both_generations` | LIN |
| A10 | callbacks execute outside kill-state lock | `test_kill_switch_reentrancy.py::test_listener_can_call_trip_without_recursive_reinvocation` (lock-reentrancy proof) | LIN |
| A11 | epoch bump cannot race past final mint/consume | `test_m5_epoch_linearization.py::test_direct_epoch_bump_linearizes_after_final_permit_mint` | LIN |
| A12 | fenced unused expiry persists NO_EFFECT before eviction | `test_m5_first_pass_regressions.py::test_expired_fenced_permit_terminalizes_canonical_attempt` | LIN |
| A13 | permit expiry blocked on authority checks re-evaluated at consume | `test_m5_consume_ttl_linearization.py::test_permit_expiring_while_policy_fence_blocks_is_not_consumed` | LIN |
| A14 | outcome APIs require canonical attempt object | `test_m5_first_pass_regressions.py::test_outcome_rejects_reconstructed_attempt_even_with_matching_lineage` | LIN |
| A15 | caller evidence cannot forge reserved correlation keys | `test_m6_reconciliation_ledger.py::test_evidence_correlation_like_keys_cannot_shadow_record_identity`; `::test_record_evidence_is_deeply_detached_from_caller_mutation` (L8-MF13 correction: the previously cited execution-models function does not exist in that file, and the reservation-retry namesake establishes effect_id immutability, not correlation-key forging) | LIN |
| A16 | corrupt tail / malformed schema / lineage / transitions fail closed | `test_effect_ledger.py::test_read_corrupt_tail_fails_closed`; `test_effect_ledger_schema.py::test_optional_lineage_fields_must_be_none_or_nonempty_strings`; `test_m5_recovery_guard.py::test_corrupt_ledger_makes_guard_unavailable` | LIN (schema/corruption files also S25) |
| A17 | same-path ledger writers serialized process-locally | `test_m6_layer6_windows_durability.py::test_windows_case_variant_effect_paths_share_one_writer_lock`; `::test_windows_case_variant_reconciliation_paths_share_one_state` | S25 |
| A18 | bytes-written/fsync-failed exact-fact retry without duplicates | `test_effect_ledger_durability_retry.py::test_retry_same_initial_reservation_redurables_without_duplicate`; `::test_retry_same_terminal_fact_redurables_without_duplicate`; `::test_retry_with_changed_evidence_is_not_silently_accepted` | LIN + S25 |
| A19 | ambiguous reservation retry uses stable effect_id | `test_m5_reservation_retry_identity.py::test_written_then_fsync_failed_reservation_retries_same_effect_fact` | LIN |
| A20 | clean release denied after reservation I/O starts | `test_execution_models.py::test_reservation_started_cannot_use_clean_no_effect_release` | LIN |
| A21 | terminal evidence strict JSON; persistence failure retryable | `test_effect_ledger_schema.py::test_details_reject_values_json_would_coerce_or_encode_nonportably`; `test_m5_reservation_retry_identity.py::test_invalid_terminal_evidence_fails_without_losing_retryable_outcome` | LIN (schema file also S25) |
| A22 | grant/attempt lifecycle fields reject direct mutation | `test_m6_confirmation_state_hardening.py::test_mutating_returned_token_cannot_resurrect_stale_epoch`; `::test_mutating_consumed_mirror_cannot_restore_single_use_authority`; `test_execution_models.py::test_spent_grant_is_irrevocably_terminal` | LIN |
| A23 | final grant validation + spend = one lifecycle operation | `test_m5_atomic_grant_spend.py::test_spend_if_live_refreshes_expiry_at_transition` | LIN |
| A24 | grant expiry at final atomic spend denied before mint; fenced close NO_EFFECT | `test_m5_atomic_grant_spend.py::test_gateway_rejects_grant_expiring_at_atomic_spend` | LIN |
| A25 | slow grant-clock validation cannot consume returned permit TTL | `test_m5_atomic_grant_spend.py::test_slow_grant_spend_does_not_consume_new_permit_ttl` | LIN |
| A26 | SAFE_* assignments require broker-level replay evidence | `test_m6_layer7_replay_safety.py::test_layer7_does_not_promote_like_or_unlike_without_external_replay_evidence`; `::test_supported_live_factory_uses_layer7_qualified_broker` | LIN |
| A27 | unproven mutation families stay UNKNOWN/REQUIRED | `test_m5_first_pass_regressions.py::test_unimplemented_future_actions_remain_conservatively_fenced`; `test_kernel_risk_gate.py::test_unknown_action_type_is_denied` | LIN |
| A28 | caller intent mutation during blocked reservation cannot alter lineage | `test_m5_intent_snapshot_and_mint_time.py::test_authorize_uses_one_snapshot_if_caller_mutates_intent_during_fsync` | LIN |
| A29 | missing target type/id denied before REQUIRED reservation or BEST_EFFORT mint | `test_m5_target_binding.py::test_best_effort_empty_target_is_denied_before_authority` (asserts `target_missing`); the write-broker binding seam (`test_m5_write_broker.py` `target_missing` returns) is the shared denial point for both paths (L8-MF13 correction, replacing the claim-fence misattribution) | LIN |
| A30 | permit time sampled only after slow reservation/grant/kill work | `test_m5_intent_snapshot_and_mint_time.py::test_permit_ttl_starts_when_permit_is_actually_minted` | LIN |
| A31 | grant expiry during reservation uses the grant clock | `test_m5_grant_store_pruning.py::test_next_mint_prunes_elapsed_active_grants`; `test_m5_monotonic_authority_clock.py::test_default_grant_authority_clock_is_monotonic` | LIN |
| A32 | default clocks monotonic; ledger timestamps UTC | `test_m5_monotonic_authority_clock.py` (all three functions) | LIN |
| A33 | failed pre-permit cancellation leaves reservation unresolved, mints nothing | `test_m5_intent_snapshot_and_mint_time.py::test_failed_prepermit_close_leaves_reservation_unresolved_and_no_authority` | LIN |

## 3. M6 R1–R48 (docs/M6_DESIGN.md §20) — function-level

| Reqs | Establishing test function(s) | Axis | Evidence |
|---|---|---|---|
| R1 | `test_m6_recovery_projector.py::test_no_reconciliation_preserves_m5_settled_and_unresolved_meaning` | OS | S25 |
| R2 | `test_m6_layer5_restart.py::test_r2_reserved_is_unknown_and_blocked_after_true_process_restart` | OS | S25 |
| R3–R5 | `test_m6_reconciliation_layer4_acceptance.py::test_both_terminal_verdicts_resolve_both_reconciliable_raw_states` (L8-MF14 correction: constructs BOTH verdicts against BOTH eligible raw states — a genuine range row); `test_m6_reconciliation_coordinator.py::test_resolve_appends_fact_advances_epoch_and_clears_only_recovery_gate` (mechanism half) | — | LIN |
| R6 | **WEAK — retained:** no inspection-specific oracle found (an inconclusive INSPECTION leaving recovery blocked); the projector rows establish adjacent boundaries only | OS | — |
| R7–R10 | `test_m6_reconciliation_layer4_acceptance.py::test_unknown_effect_denies_before_epoch_or_persistence` (R7 — L8-MF14 correction, WEAK removed); R8: `test_m6_recovery_projector.py::test_every_reconciliation_lineage_field_must_match_canonical_first_effect`; R9: `test_m6_reconciliation_coordinator.py::test_existing_terminal_reconciliation_denies_fresh_authority_before_epoch` + `::test_settled_raw_effect_is_denied_before_epoch_advance`; **R10 retained WEAK**: the live-attempt family (`test_m6_reconciliation_live_attempt.py::test_reserved_live_attempt_blocks_display_and_resolution_until_terminal_outcome`) is the closest candidate, but a literal `live_attempt_owned` denial is not yet assertion-verified | — | LIN |
| R11 | `test_m6_reconciliation_strict_json.py::test_duplicate_top_level_json_key_is_corruption` (representative; the file's six functions carry the strictness family); `test_m6_reconciliation_evidence_artifacts.py::test_external_artifact_requires_lowercase_sha256` | — | LIN |
| R12, R13, R38 | `test_m6_reconciliation_authority.py::test_expired_uncommitted_authority_...` family — `::test_committed_exact_fact_survives_later_ttl_expiry` (R13/R38), `::test_uncommitted_authority_expires_on_monotonic_clock` (R12); `test_m6_reconciliation_committed_continuation.py::test_clean_failure_pins_exact_fact_across_same_path_coordinators` | — | LIN |
| R14, R15 | `test_m6_confirmation_epoch.py::test_epoch_advance_revokes_all_older_tokens_including_unrelated` (R15); `::test_kernel_rejects_old_epoch_token_before_execute` (R14) | — | LIN |
| R16 | `test_m6_reconciliation_committed_continuation.py::test_same_path_coordinator_rejects_second_confirmation_epoch` | — | LIN |
| R17, R18, R19, R21, R35 | `test_m6_reconciliation_ledger_durability.py::test_written_row_with_failed_file_fsync_latches_ambiguity`; `::test_exact_retry_redurables_visible_fact_and_clears_latch`; `::test_same_path_instance_observes_ambiguity_and_blocks_different_fact` | OS | LIN + S25 |
| R20, R34 | `test_m6_layer5_restart.py::test_r20_r34_durable_reconciliation_survives_process_crash_before_publication` | OS | S25 |
| R22, R23, R24 | R22: `test_m6_reconciliation_ledger.py::test_second_terminal_fact_for_same_effect_is_rejected` (L8-MF14 correction — the contradictory-terminal fail-closed oracle); R23: `test_m6_recovery_independent_gates.py::test_dispatcher_corrupt_reconciliation_fails_before_browser_start` (L8-MF14 correction); R24: `test_effect_ledger.py::test_read_corrupt_tail_fails_closed` | OS | R23/R24: S25; R22: LIN |
| R25 | `test_m6_layer5_qualification.py::test_r25_forged_invocation_journal_data_has_no_recovery_authority` | — | LIN |
| R26, R27 | `test_m6_recovery_projector.py::test_one_reconciled_effect_cannot_clear_another_same_key_unknown` | OS | S25 |
| R28 | `test_m6_layer5_restart.py::test_clean_no_row_failure_loses_committed_authority_across_restart` (old authority dead); `test_m6_reconciliation_operator.py::test_expired_uncommitted_authority_requires_fresh_confirmation` | OS | S25 |
| R29 | `test_m6_layer5_qualification.py::test_r29_competing_coordinator_verdicts_have_one_terminal_winner`; `test_m6_layer5_sibling_coordinator.py::test_r29_same_path_sibling_coordinators_share_one_terminal_order` | — | LIN |
| R30 | `test_m6_reconciliation_operator.py::test_unconfirmed_proposal_cannot_resolve`; `::test_prepare_is_read_only_and_does_not_mint_authority` | — | LIN |
| R31, R32 | R31 **retained WEAK** (an inspection-read FAILURE producing inconclusive/no-authority lacks an isolated oracle); R32 (L8-MF14 refinement, WEAK removed): `test_m5_leased_read_broker.py::test_read_navigation_fails_closed_while_composer_is_owned`; `test_m5_evidence_reader.py::test_reader_refuses_navigation_while_content_context_is_owned` | SDK(stub) | LIN |
| R33 | `test_m6_recovery_publication_fail_closed.py::test_any_projection_exception_discards_previously_cached_clear_authority` | — | LIN |
| R36, R37 | `test_m6_layer5_restart.py::test_r36_fresh_process_startup_redurabilizes_surviving_reconciliation`; `::test_r37_fresh_process_startup_redurability_failure_fails_closed` | OS | S25 |
| R39 | `test_m6_layer5_qualification.py::test_r39_reconciliation_wins_then_old_token_is_stale_after_clear`; `::test_r39_write_refresh_wins_and_observes_old_blocked_truth` | — | LIN |
| R40 | `test_m6_confirmation_epoch.py::test_consume_vs_epoch_advance_has_one_synchronized_order` | — | LIN |
| R41 | `test_m6_recovery_independent_gates.py::test_recovery_clear_does_not_bypass_live_dedupe` | — | LIN |
| R42 | `test_m6_recovery_independent_gates.py::test_recovery_clear_does_not_refund_exhausted_token_bucket`; `test_write_kernel.py::test_token_bucket_global_circuit_breaker_blocks_mixed_actions` | — | LIN |
| R43 | `test_m6_reconciliation_independent_gates.py::test_kill_may_remain_tripped_while_local_reconciliation_completes` (L8-MF10 correction; the drain-handoff tests do not establish R43); `test_m6_recovery_independent_gates.py::test_recovery_clear_does_not_reset_or_bypass_kill` | — | LIN |
| R44 | `test_m6_layer5_qualification.py::test_r44_old_reconciliation_fact_has_no_ttl_or_audit_style_rotation` (L8-MF10 correction) | — | LIN |
| R45 | `test_m6_reconciliation_independent_gates.py::test_reconciliation_id_collision_denies_before_epoch_or_authority_commit` | — | LIN |
| R46 | `test_m6_layer6_windows_durability.py::test_windows_platform_fingerprint_is_real_target_platform` + the file's durability functions (the row's S25 claim is exact: this file IS the Windows job's selection) | OS | S25 |
| R47 | `test_m6_confirmation_epoch.py::test_wall_clock_jumps_do_not_change_token_validity` | — | LIN |
| R48 | `test_m6_confirmation_epoch.py::test_expiry_is_sampled_after_second_phase_preview` | — | LIN |

## 4. M6 §20 additional mandatory regressions — M6-A1..A22, function-level

| ID(s) | Bullet (abbreviated) | Establishing test function(s) | Evidence |
|---|---|---|---|
| A1 | evidence hash deterministic; mismatch rejected | `test_m6_reconciliation_ledger.py::test_hash_is_canonical_across_object_key_order_and_unicode`; `::test_record_rejects_hash_mismatch_and_nonlowercase_digest` (L8-MF15 correction — the artifact-identity test establishes a different invariant) | LIN |
| A2 | strict JSON + non-empty identity | `test_m6_reconciliation_strict_json.py` (six functions; e.g. `::test_blank_line_is_corruption_not_silently_skipped`); `test_m6_reconciliation_ledger.py::test_evidence_requires_nonempty_structured_minimum` | LIN |
| A3 | reserved evidence keys cannot shadow record identity | `test_m6_reconciliation_ledger.py::test_evidence_correlation_like_keys_cannot_shadow_record_identity` (L8-MF15 correction) | LIN |
| A4 | same-path reconciliation writers serialized | `test_m6_layer6_windows_durability.py::test_windows_case_variant_reconciliation_paths_share_one_state` | S25 |
| A5 | publication cannot regress newer blocked/unavailable truth to older clear truth | `test_m6_recovery_projector.py::test_same_recovery_domain_guards_share_publication_serialization` (publication ordering is serialized — no torn/regressing publication); `test_m6_recovery_publication_fail_closed.py::test_any_projection_exception_discards_previously_cached_clear_authority` (a cached clear cannot survive a failed projection); the historical-preservation half: `test_m6_recovery_projector.py::test_reconciliation_does_not_rewrite_m5_historical_projection` (L8-MF15 correction — flagged: the ordering invariant is carried by serialization + discard, not by a dedicated newer-blocked-vs-older-clear scenario) | LIN |
| A6 | one effect's reconciliation cannot clear another same-key unknown | `test_m6_recovery_projector.py::test_one_reconciled_effect_cannot_clear_another_same_key_unknown` | S25 |
| A7 | no Dispatcher capability obtains ReconciliationAuthority | `test_m6_reconciliation_coordinator.py::test_foreign_direct_authority_cannot_bypass_operator_confirmation` | LIN |
| A8 | read-only inspection performs no external mutation | `test_m6_reconciliation_operator.py::test_prepare_is_read_only_and_does_not_mint_authority` | LIN |
| A9 | operator identity distinct from X actor identity | `test_m6_reconciliation_operator.py::test_exact_same_session_confirmation_mints_one_authority` (binding) + `test_kernel_hygiene.py::test_whoami_hook_binds_actor_identity` — **flagged WEAK for review: no single function isolates the distinction** | LIN |
| A10 | authority monotonic expiry; not persisted across restart | `test_m6_reconciliation_authority.py::test_uncommitted_authority_expires_on_monotonic_clock`; `test_m6_layer5_restart.py::test_clean_no_row_failure_loses_committed_authority_across_restart` | S25 (restart half) |
| A11 | kernel tokens monotonic; wall-clock diagnostic only | `test_m6_confirmation_epoch.py::test_issue_captures_epoch_and_separates_wall_diagnostics_from_authority` | LIN |
| A12 | confirmation expiry sampled at synchronized consume boundary | `test_m6_confirmation_epoch.py::test_consume_vs_epoch_advance_has_one_synchronized_order` | LIN |
| A13 | confirmation epoch process-local; fresh process = no pending tokens | `test_m6_confirmation_epoch.py::test_new_confirmation_state_has_no_old_process_authority` | LIN |
| A14 | clean persistence failure re-drives only the exact frozen fact | `test_m6_reconciliation_committed_continuation.py::test_clean_failure_pins_exact_fact_across_same_path_coordinators` | LIN |
| A15 | restart after clean no-row failure requires fresh confirmation | `test_m6_layer5_restart.py::test_clean_no_row_failure_loses_committed_authority_across_restart` | S25 |
| A16 | positive evidence cannot infer causal identity from content equality unless the predicate is qualified | `test_m6_layer7_replay_safety.py::test_terminal_like_evidence_uses_same_qualified_state_reader` (qualified predicates only); `::test_state_change_after_gate_never_falls_through_to_wrong_direction` (L8-MF12 correction: split from the merged row) | LIN |
| A17 | negative evidence cannot infer no-effect from failed observation | `test_m6_layer7_replay_safety.py::test_layer7_does_not_promote_like_or_unlike_without_external_replay_evidence`; `::test_non_authoritative_controls_fail_before_commit` (L8-MF12 correction: its own row) | LIN |
| A18 | missing reconciliation file is valid empty history; corruption is not | `test_m6_recovery_projector.py::test_missing_reconciliation_file_is_valid_empty_and_keeps_unknown_blocked` | S25 |
| A19 | reconciliation row with missing EffectLedger target is corruption | `test_m6_recovery_projector.py::test_reconciliation_targeting_missing_effect_is_composite_corruption` | S25 |
| A20 | audit-journal absence/corruption is irrelevant | `test_m6_layer5_qualification.py::test_r25_forged_invocation_journal_data_has_no_recovery_authority` | LIN |
| A21 | M6 does not alter M5 monotonic grant/permit clocks | `test_m5_monotonic_authority_clock.py` (all three functions, green under full-suite composition with M6) | LIN |
| **A22** | **M6 does not weaken M5 kill/epoch/policy/permit validation** | Bounded composition verification, per the review's conditional acceptance: the constituent M5 protections are the sections-1/2 rows (`test_kill_switch.py`, `test_m5_epoch_linearization.py`, `test_kernel_risk_gate.py`, `test_m5_atomic_grant_spend.py`, `test_scoped_authority_guards.py` — green with M6 composed), plus M6-side non-interference witnesses `test_m6_recovery_independent_gates.py::test_recovery_clear_does_not_reset_or_bypass_kill`; `::test_recovery_clear_does_not_bypass_live_dedupe`; `::test_recovery_clear_does_not_refund_exhausted_token_bucket`. NOT a claim that one integration test proves every M5 property | LIN |

## 5. M7 §23 mandatory regressions — M7-A1..A13

| ID | Bullet | Establishing test function(s) | Evidence |
|---|---|---|---|
| A1 | all M5 T1–T25 + §14 regressions under one owner | **EXPLICIT CROSS-REFERENCE:** sections 1–2 above (every row) | per row |
| A2 | all M6 R1–R48 + §20 regressions under one owner | **EXPLICIT CROSS-REFERENCE:** sections 3–4 above (every row) | per row |
| A3 | acquisition failure → no token/fact/browser/reconciliation | `test_m7_layer2_runtime_wiring.py::test_failed_start_releases_ownership`; `::test_loser_fails_busy_before_browser_then_retries` | LIN |
| A4 | stale owner instance cannot mint/consume via IPC | `test_m7_layer5_transport.py::test_stale_owner_instance_token_cannot_reach_execution`; `test_m7_layer3_instance_lifecycle.py::test_stale_reference_denied_under_a_successor_session`; L7: `test_m7_layer7_ipc_faults.py` stale-instance negatives | LIN + W11 |
| A5 | request-cache eviction grants no replay authority | `test_m7_layer5_request_table.py::test_completed_entries_evict_bounded_no_exactly_once_claim` | LIN |
| A6 | table pressure cannot evict/orphan admitted work | `test_m7_layer5_request_table.py::test_inflight_entries_are_pinned_at_capacity`; L7 tablesat scenario | LIN + W11 |
| A7 | journal audit-only; never owner identity | `test_journal.py::test_journal_append_real_io_failure_does_not_raise` + audit-redaction functions; `test_m6_layer5_qualification.py::test_r25_forged_invocation_journal_data_has_no_recovery_authority` | LIN |
| A8 | instance id never changes M5/M6 lineage | `test_m7_layer3_instance_lifecycle.py::test_instance_id_is_fresh_256bit_per_session`; `::test_clean_reacquisition_mints_a_different_id_same_domain` | LIN |
| A9 | restart ≠ durable claim from bucket/dedupe reset | `test_write_kernel.py::test_token_bucket_blocks_over_per_action_limit`; `::test_dedupe_blocks_duplicate_within_ttl` (process-local semantics) + `test_m6_confirmation_epoch.py::test_new_confirmation_state_has_no_old_process_authority` | LIN |
| A10 | offline reconciliation keeps operator confirmation | `test_m7_layer5_reconciliation_routing.py::test_external_recovery_under_live_owner_fails_busy_before_m6`; `::test_owner_absent_standalone_recovery_remains_the_temporary_owner`; `test_m6_reconciliation_operator.py::test_expired_uncommitted_authority_requires_fresh_confirmation` | LIN |
| A11 | raw diagnostics outside owner never mutation authority | `test_m5_no_legacy_write_route.py::test_future_unmigrated_write_is_security_denied_before_compose`; `::test_registered_remote_write_surface_is_fully_m5_migrated` | LIN |
| A12 | media ingress digest/manifest binding + preflight semantics | `test_m7_layer6_ingress.py` one-handle functions; `test_m7_layer6_real_stack.py::test_T68_preview_binds_digest_confirm_reaches_owner_bytes`; `::test_T70_corrupt_item_n_means_zero_attach_calls` | LIN (POSIX half) + W11 (Windows one-handle) → E-2 |
| A13 | genuine sibling processes, not two objects in one interpreter | L7 harness family (process-scale; functions enumerated in the candidate matrix M-1..M-5 rows) | L7P + W11 → M-1..M-5 |

## 6. Flagged items requiring the review's decision (refined per the v2 review)

The register's standing verification rule (adopted from the v2 review): *a cited test must construct the relevant condition, cross the frozen decision boundary, and assert the exact required outcome — a real function name whose assertions establish a different invariant is not evidence; for negative security requirements the denial must arise from the specific prohibited condition.*

1. **R6** — no inspection-specific oracle (inconclusive INSPECTION leaving recovery blocked); WEAK, retained.
2. **R10** — live-attempt family is the closest candidate; a literal `live_attempt_owned` denial is not assertion-verified; WEAK, retained.
3. **R31** — inspection-read failure → inconclusive/no-authority lacks an isolated oracle; WEAK, retained.
4. **M6-A5** — the no-regression ordering invariant is carried by publication serialization + cached-clear discard, not a dedicated newer-blocked-vs-older-clear scenario; flagged.
5. **M6-A22** — bounded composition verification ACCEPTED by the review conditionally on the corrected constituent mappings (now done); retained as composition-labeled, never as a single-test claim.
6. **RESOLVED (removed from WEAK):** R7 (direct test mapped), R32 (direct leased-read/evidence-reader tests mapped), M6-A9 remains combination-carried but now names its distinctness evidence: `test_m6_reconciliation_operator.py` binds OPERATOR session confirmation while `test_kernel_hygiene.py::test_whoami_hook_binds_actor_identity` binds the X ACTOR — the two identities are separately established; flagged only if the review requires a single isolating function.
