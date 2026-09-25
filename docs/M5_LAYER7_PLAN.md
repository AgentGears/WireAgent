# M5 Layer 7 — Journal Audit-Only Migration Plan

**Status:** COMPLETE — merged as PR #8  
**Base:** Layer 6 squash `03df4d3458828cc131989fd44fe27d48cef1240c`  
**Final candidate:** `390967d96f87cdb483334bd8a2120629312df3f4`  
**Squash merge:** `0c62402ae01b50d7662b3978cbf2bee4109aa035`  
**Scope:** retire the invocation journal as a live safety input while preserving audit/diagnostic evidence

Layer 7 completed the M5 build order. Layers 5 and 6 moved supported browser mutations through scoped M5 authority and made `EffectLedger` + `RecoveryGuard` authoritative for unresolved external-effect replay safety. Layer 7 removes the remaining transitional startup hydration of `DedupeStore` and `TokenBucket` from `.webwire/journal.ndjson`.

## 1. Authority boundary

After Layer 7:

```text
.webwire/effects.ndjson
    durable M5 effect facts
    fsync-backed
    fail-closed on corruption
    RecoveryGuard input

.webwire/journal.ndjson
    invocation audit / diagnostics only
    best-effort output
    never read to authorize, deny, dedupe, rate-limit, or recover a mutation
```

The invocation journal is not a second safety database. Its writer is best-effort, so it cannot serve as authoritative M5 recovery or policy-state persistence.

## 2. Process-local controls after retirement

`DedupeStore` and `TokenBucket` remain defense-in-depth controls during a running process, but their windows are explicitly process-local:

- dedupe blocks the same semantic key inside its configured TTL while the process is alive;
- token buckets enforce per-action and global limits while the process is alive;
- process restart resets those in-memory windows;
- restart does **not** reset M5 unresolved-effect replay safety, because `RecoveryGuard` hydrates from `EffectLedger` before browser startup and refreshes before supported M5 writes;
- a new human approval after restart is not treated as an automatic replay solely because a previous confirmed effect had the same semantic key.

This is an explicit boundary, not an accidental loss of durability. Cross-restart budgets/dedupe would require their own durable policy store and durability contract; the best-effort audit journal is not that store.

## 3. Implemented steps

1. Removed live Dispatcher startup hydration from `journal.ndjson`.
2. Removed journal-coupled hydration APIs from `DedupeStore` and `TokenBucket`.
3. Removed `read_recent_write_records()` from the supported journal API.
4. Retained write-fact fields (`action_type`, `risk_tier`, `dedupe_key`) as audit evidence only.
5. Changed Dispatcher audit labeling to record the shaped WriteKernel verdict when available instead of overloading `policy_decision="allowed"`.
6. Added regressions proving journal content cannot affect a fresh Dispatcher's dedupe/budget state or RecoveryGuard decision.
7. Preserved journal append/rotation/redaction/screenshot behavior as best-effort audit functionality.
8. Completed maintainer-first review, exact-head CI, and the repository's independent-review fallback after Codex quota exhaustion.

## 4. Required invariants

1. No supported mutation path reads `journal.ndjson` before deciding whether external mutation may occur.
2. Missing, corrupt, truncated, stale, or forged journal content cannot clear or create M5 recovery authority.
3. `EffectLedger` remains the sole durable M5 effect-fact authority.
4. `RecoveryGuard` remains the sole restart replay-denial projection for unresolved M5 effects.
5. Journal append failure cannot change commit/outcome/recovery truth.
6. Dedupe and token buckets remain active within one process.
7. Restart intentionally resets only those process-local dedupe/rate windows, not unresolved-effect safety.
8. Journal rotation/retention remains an audit concern and cannot affect mutation authority.
9. No journal field, including `policy_decision` or `dedupe_key`, is consumed as an execution permit, recovery fact, or durable safety fact.
10. Layer 7 does not add automatic reconciliation or rewrite `EFFECT_UNKNOWN` history.

## 5. Acceptance evidence

Final exact-head CI: GitHub Actions **#366** on candidate `390967d96f87cdb483334bd8a2120629312df3f4`.

- Python 3.11: **765 tests passed**;
- Python 3.12: green;
- Ruff: all checks passed;
- mypy: no issues in **78 source files**;
- PR merge-ref integration tested exact candidate against Layer-6 `main`.

Maintainer-first review identified and reconciled six issues before independent review:

1. dormant Dispatcher journal hydration coupling;
2. misleading always-empty compatibility reader;
3. leftover dedupe/token-bucket hydration APIs;
4. stale README restart-budget claims;
5. stale living `STATE.md` architecture status;
6. misleading journal `policy_decision="allowed"` semantics.

Codex then attempted review but the repository code-review usage limit was exhausted. No GitWire review or inline thread appeared. Per the standing review rule, the maintainer performed a distinct adversarial second pass. That pass found one additional coverage defect: the journal rotation regression had been accidentally removed with the legacy hydration suite. The final candidate restores explicit regressions for rotation, URL redaction, screenshot policy, and genuine append-I/O failure; exact-head CI #366 revalidated the result.

Acceptance regressions cover:

- a populated prior journal does not hydrate a fresh Dispatcher's `DedupeStore` or token budget;
- an unresolved matching EffectLedger fact still blocks after restart even when the journal is absent;
- arbitrary/fabricated journal write facts do not create a RecoveryGuard block;
- corrupt journal content does not change RecoveryGuard projection;
- in-process duplicate and token-bucket enforcement still work without journal hydration;
- journal append failure remains best-effort/non-authoritative;
- journal rotation preserves old/new records;
- audit URL redaction strips query/fragment material;
- screenshot policy behavior remains pinned.

## 6. Out of scope

- a new durable rate-limit database;
- durable cross-restart semantic dedupe for confirmed effects;
- automated reconciliation;
- rewriting terminal `EFFECT_UNKNOWN`;
- cross-process browser/ledger coordination;
- hostile Python sandboxing;
- distributed exactly-once semantics.

Layer 7 is complete. With PR #8 merged, the M5 build order (Layers 1–7) is complete in `main`.
