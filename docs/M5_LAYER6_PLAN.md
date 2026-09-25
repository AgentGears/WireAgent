# M5 Layer 6 — RecoveryGuard Plan

**Status:** implementation starting  
**Base:** Layer 5 squash `515999740bb7b5fa4ad9c74582d6166157e9075b`  
**Scope:** make durable unresolved M5 effect facts authoritative replay-denial state before any supported live mutation reaches browser interaction

Layer 6 turns the `EffectLedger` recovery projection into an enforced live-runtime gate. It does not reconcile effects and it does not rewrite ledger history. It only converts durable uncertainty into a deterministic fail-closed denial until an explicit future reconciliation design supplies evidence-bearing resolution semantics.

## 1. Normative contract

The frozen `M5_DESIGN.md` requires:

```text
EffectLedger
  -> derive unresolved RESERVED / EFFECT_UNKNOWN semantic keys
  -> hydrate RecoveryGuard
  -> deny matching mutation before browser interaction
```

The denial is `reconciliation_required`. Health/status reporting is diagnostic only and cannot substitute for enforcement.

Raw `RESERVED` projects to effective `EFFECT_UNKNOWN`. Explicit `EFFECT_UNKNOWN` remains terminal. `NO_EFFECT` and `EFFECT_CONFIRMED` do not block replay through RecoveryGuard.

## 2. Review findings that shape implementation

1. **Startup hydration alone is insufficient.** Layer 5 can durably create `EFFECT_UNKNOWN` while the process survives. In particular, an exception/cancellation after authority crosses can leave canonical unresolved M5 state even when the transitional WriteKernel never records legacy dedupe state. RecoveryGuard therefore hydrates at startup **and refreshes from the ledger before every supported M5 write policy pass**.
2. **Recovery authority must fail closed.** `EffectLedger` corruption/read failure cannot be treated as an empty unresolved set. Startup fails before browser launch, and a later refresh failure denies mutation as reconciliation-required/unavailable.
3. **The gate belongs before preview.** `WriteKernel.compose()` is declarative and browser-free; preview may navigate. The guard check therefore runs after intent/registry validation and before token-bucket, dedupe, dry-run preview, confirmation preview, or execution.
4. **Only real M5 mutation routes are guarded.** `compose_post` is a deliberate no-effect shell whose semantic `action_type` is still `post`; it must bypass RecoveryGuard so unresolved public-post state does not disable a non-mutating design/preview tool.
5. **Semantic identity is exact.** RecoveryGuard uses the exact `WriteIntent.dedupe_key()` representation already persisted by `CommitGateway` as `semantic_key`. It does not broaden matching by action, actor, target, or text independently.
6. **No reconciliation mutation in Layer 6.** The guard may expose diagnostic snapshots, but it cannot append a replacement terminal state or clear `EFFECT_UNKNOWN`.

## 3. Implementation surface

1. Add `webwire.safety.recovery_guard` with:
   - startup `hydrate()` from `EffectLedger.recovery_projection()`;
   - `refresh()` using the same canonical projection;
   - exact semantic-key lookup;
   - immutable diagnostic block records containing effect IDs/raw states;
   - fail-closed unavailable state when refresh cannot trust the ledger.
2. Inject one RecoveryGuard into the Dispatcher alongside the existing `EffectLedger`/CommitGateway.
3. Hydrate it at the start of `Dispatcher.start()` **before** `SessionManager.start()` so corrupt recovery authority cannot launch the live browser runtime.
4. Inject the guard into `WriteKernel` and check it immediately after intent + risk-registry validation, before any browser-capable stage.
5. Refresh on each guarded M5 write attempt so same-process newly unresolved effects are blocked without requiring restart.
6. Bypass only `compose_post`, because it has no remote mutation path.
7. Preserve the invocation journal's transitional dedupe/budget hydration until Layer 7; RecoveryGuard is independent of that journal.

## 4. Denial contract

For a blocked semantic replay:

```text
policy.verdict    = deny
policy.blocked_by = reconciliation_required
reconciliation_required = true
semantic_key      = exact blocked key
```

No preview, confirmation-token issuance, scoped authority creation, broker mutation, or browser navigation may occur after this gate.

If recovery state cannot be refreshed safely, the write is also denied fail-closed with reconciliation required. The runtime must never reinterpret a ledger failure as "no unresolved effects".

## 5. Acceptance surface

Layer 6 is not complete until regressions prove at least:

1. empty ledger -> no block;
2. `RESERVED` -> blocked after restart hydration;
3. `EFFECT_UNKNOWN` -> blocked;
4. `RESERVED -> NO_EFFECT` -> not blocked;
5. `RESERVED -> EFFECT_CONFIRMED` -> not blocked;
6. exact semantic-key match blocks while a different semantic key remains allowed;
7. malformed/corrupt ledger fails closed;
8. startup hydration failure occurs before browser session start;
9. blocked invocation performs no preview/browser interaction and issues no confirmation token;
10. an unresolved fact appended **after startup** is picked up by per-write refresh and blocks the next matching invocation;
11. `compose_post` remains usable as a no-effect shell even when the equivalent real post semantic key is unresolved;
12. journal hydration state does not weaken or replace RecoveryGuard authority.

## 6. Deliberately deferred

- automatic external reconciliation;
- evidence-bearing resolution of terminal `EFFECT_UNKNOWN`;
- Layer 7 retirement of journal-backed dedupe/budget hydration;
- cross-process ledger writers or browser serialization;
- distributed exactly-once semantics.

## 7. Review gate

Implementation is reviewed maintainer-first against the exact branch head. Only after all first-pass findings and CI are reconciled may an independent second-opinion review be requested. Layer 6 must not weaken any Layer 1-5 invariant.
