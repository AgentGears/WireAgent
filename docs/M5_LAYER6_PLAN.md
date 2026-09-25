# M5 Layer 6 — RecoveryGuard Plan

**Status:** implementation complete; maintainer-first review gate in progress  
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
4. **Only real M5 mutation routes are guarded.** `compose_post` is a deliberate no-effect shell whose semantic `action_type` is still `post`; it may bypass RecoveryGuard so unresolved public-post state does not disable a non-mutating design/preview tool, but that exemption must not become transferable authority for `post_text`.
5. **Semantic identity is exact.** RecoveryGuard uses the exact `WriteIntent.dedupe_key()` representation already persisted by `CommitGateway` as `semantic_key`. It does not broaden matching by action, actor, target, or text independently.
6. **No reconciliation mutation in Layer 6.** The guard may expose diagnostic snapshots, but it cannot append a replacement terminal state or clear `EFFECT_UNKNOWN`.

## 3. Maintainer-first findings discovered during implementation

The first-pass review found three material authority defects before any independent second-opinion review was requested:

1. **F1 — stale concurrent RecoveryGuard publication.** The initial implementation read the ledger outside the guard lock and locked only cache publication. Two refreshes could therefore publish snapshots out of order, allowing an older clear snapshot to overwrite a newer unresolved snapshot. The complete ledger-read -> projection -> publication sequence is now serialized under one guard lock, with a deterministic concurrent-refresh regression.
2. **F2 — caller-controlled RecoveryGuard exemption.** The first kernel shape exposed a generic `enforce_recovery_guard=False` bypass. That was too broad for a safety gate. Exemption authority now comes only from the kernel's internal `compose_post` allowlist; an explicit false flag for any other capability is ignored and regression-tested.
3. **F3 — confirmation token transferable across equal-intent capabilities.** `compose_post` and `post_text` intentionally create the same `WriteIntent` for the same actor/text, but the legacy confirmation token bound only the intent hash. A token issued by the no-effect compose shell could therefore be presented to the live post capability. Confirmation tokens now bind **capability name + immutable intent hash + risk tier**. Generic equal-intent and concrete `compose_post` -> `post_text` regressions prove the token cannot cross that authority boundary.

These findings are part of Layer 6 because the no-effect-shell RecoveryGuard exemption is safe only if confirmation authority cannot be transferred to a mutating capability.

## 4. Implementation surface

1. Add `webwire.safety.recovery_guard` with:
   - startup `hydrate()` from `EffectLedger.recovery_projection()`;
   - `refresh()` using the same canonical projection;
   - exact semantic-key lookup;
   - immutable diagnostic block records containing effect IDs/raw states;
   - fail-closed unavailable state when refresh cannot trust the ledger;
   - serialized read/projection/publication ordering so stale snapshots cannot overtake newer recovery truth.
2. Inject one RecoveryGuard into the Dispatcher alongside the existing `EffectLedger`/CommitGateway.
3. Hydrate it at the start of `Dispatcher.start()` **before** `SessionManager.start()` so corrupt recovery authority cannot launch the live browser runtime.
4. Inject the guard into `WriteKernel` and check it immediately after intent + risk-registry validation, before any browser-capable stage.
5. Refresh on each guarded M5 write attempt so same-process newly unresolved effects are blocked without requiring restart.
6. Exempt only the internally named `compose_post` no-effect shell; callers cannot disable RecoveryGuard for another capability.
7. Bind confirmation tokens to the exact capability as well as the immutable intent so no-effect-shell approval cannot authorize a live mutation route.
8. Preserve the invocation journal's transitional dedupe/budget hydration until Layer 7; RecoveryGuard is independent of that journal.

## 5. Denial contract

For a blocked semantic replay:

```text
policy.verdict    = deny
policy.blocked_by = reconciliation_required
reconciliation_required = true
semantic_key      = exact blocked key
```

No preview, confirmation-token issuance, scoped authority creation, broker mutation, or browser navigation may occur after this gate.

If recovery state cannot be refreshed safely, the write is also denied fail-closed with reconciliation required. The runtime must never reinterpret a ledger failure as "no unresolved effects".

A confirmation token is valid only for the capability that issued it and the exact immutable intent/risk binding. Equal intent hashes across different capability surfaces are not interchangeable authority.

## 6. Acceptance surface

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
11. concurrent refreshes cannot publish an older recovery snapshot after a newer one;
12. a caller cannot disable RecoveryGuard for a non-exempt capability;
13. `compose_post` remains usable as a no-effect shell even when the equivalent real-post semantic key is unresolved;
14. a `compose_post` confirmation token cannot authorize `post_text`, even though both currently produce the same intent hash for equal actor/text input;
15. journal hydration state does not weaken or replace RecoveryGuard authority.

## 7. Deliberately deferred

- automatic external reconciliation;
- evidence-bearing resolution of terminal `EFFECT_UNKNOWN`;
- Layer 7 retirement of journal-backed dedupe/budget hydration;
- cross-process ledger writers or browser serialization;
- distributed exactly-once semantics;
- changing the pre-existing actor/semantic-key representation; Layer 6 consumes the exact semantic key already persisted by Layer 3/5 rather than silently migrating durable identity.

## 8. Review gate

Implementation is reviewed maintainer-first against the exact branch head. Only after all first-pass findings and CI are reconciled may an independent second-opinion review be requested. Layer 6 must not weaken any Layer 1-5 invariant.
