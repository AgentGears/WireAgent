# M5 Layer 7 — Journal Audit-Only Migration Plan

**Status:** implementation in progress  
**Base:** Layer 6 squash `03df4d3458828cc131989fd44fe27d48cef1240c`  
**Scope:** retire the invocation journal as a live safety input while preserving audit/diagnostic evidence

Layer 7 completes the M5 build order. Layers 5 and 6 moved supported browser mutations through scoped M5 authority and made `EffectLedger` + `RecoveryGuard` authoritative for unresolved external-effect replay safety. The remaining transitional coupling is startup hydration of `DedupeStore` and `TokenBucket` from `.webwire/journal.ndjson`.

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

The invocation journal must not become a second safety database by accident. Its existing writer is best-effort and its legacy hydration reader is intentionally tolerant of missing/corrupt data; that behavior is incompatible with authoritative M5 recovery semantics.

## 2. Process-local controls after retirement

`DedupeStore` and `TokenBucket` remain useful defense-in-depth controls during a running process, but their windows become explicitly process-local:

- dedupe continues to block the same semantic key inside its configured TTL while the process is alive;
- token buckets continue to enforce per-action and global limits while the process is alive;
- process restart resets those in-memory windows;
- restart does **not** reset M5 unresolved-effect replay safety, because `RecoveryGuard` hydrates from `EffectLedger` before browser startup and refreshes before supported M5 writes;
- a new human approval after restart is not treated as an automatic replay solely because a previous confirmed effect had the same semantic key.

This is an explicit boundary, not an accidental loss of durability. Cross-restart budgets/dedupe would require their own durable policy store and durability contract; the best-effort audit journal is not that store.

## 3. Implementation steps

1. Remove live Dispatcher startup hydration from `journal.ndjson`.
2. Remove journal-coupled hydration APIs from `DedupeStore` and retire `TokenBucket` replay hydration from the supported runtime surface.
3. Retire `read_recent_write_records()` as a safety API; journal reading remains a diagnostics concern only.
4. Keep write-fact fields (`action_type`, `risk_tier`, `dedupe_key`) as audit evidence where available; they no longer feed execution state.
5. Clarify journal documentation: `policy_decision` is a coarse Dispatcher admission/audit field unless/until an explicit kernel-verdict field is recorded; it must never be interpreted as durable authority.
6. Add regressions proving journal content cannot affect a fresh Dispatcher's dedupe/budget state or RecoveryGuard decision.
7. Preserve existing journal append/rotation/redaction behavior as best-effort audit functionality.
8. Run exhaustive maintainer-first review, full CI, then independent second-opinion review on the exact candidate head.

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

## 5. Acceptance regressions

- A journal containing a recent matching `dedupe_key` does not hydrate a fresh Dispatcher's `DedupeStore`.
- A journal containing enough recent writes to exhaust an old token bucket does not hydrate a fresh Dispatcher's budget counters.
- An unresolved matching EffectLedger fact still blocks the same write after restart even when the journal is absent.
- A clean EffectLedger with arbitrary/fabricated journal write facts does not create a RecoveryGuard block.
- A corrupt journal does not prevent RecoveryGuard hydration or change its blocked-key projection.
- Journal append failure after a durable M5 effect fact leaves M5 safety semantics unchanged.
- In-process duplicate and token-bucket enforcement still work without journal hydration.
- Journal append, rotation, redaction, and failure-only screenshot policy continue to behave as audit features.

## 6. Out of scope

- a new durable rate-limit database;
- durable cross-restart semantic dedupe for confirmed effects;
- automated reconciliation;
- rewriting terminal `EFFECT_UNKNOWN`;
- cross-process browser/ledger coordination;
- hostile Python sandboxing;
- distributed exactly-once semantics.

Layer 7 is complete only when the live runtime has no journal-to-safety-state data path and the exact candidate has passed maintainer-first review, CI, and independent review/reconciliation.
