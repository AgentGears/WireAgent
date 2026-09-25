# M5 Layer 5 — Capability Migration Plan

**Status:** implementation complete; review gate in progress  
**Base:** Layer 4 squash `5650982`  
**Scope:** migrate supported live write execution onto scoped authority and gateway-owned outcome truth

Layer 5 turns the Layer-4 least-authority model into the supported live execution path. It does not weaken the frozen Layer-4 invariants; it removes legacy write-path authority from capability-facing code and makes terminal effect truth an explicit gateway/ledger responsibility.

## 1. Build order

1. Close the Layer-3 issued-but-unconsumed lifecycle gap with a gateway-owned proven-`NO_EFFECT` closure.
2. Define the Layer-5 execution/orchestration adapter around `AuthorizedEffect` receipts.
3. Migrate `WriteKernel` / Dispatcher construction to the supported live M5 factories.
4. Migrate write capabilities so they receive preparation/effect authorities rather than raw write broker/browser mutation surfaces.
5. Record `EFFECT_CONFIRMED` or `EFFECT_UNKNOWN` synchronously from the exact receipt permit based on evidence.
6. Coordinate read/verification navigation with the M5 browser lease.
7. Add migration/bypass regressions and retire supported legacy mutation routes.
8. Run maintainer-first exhaustive review, then an independent GitWire second-opinion pass on the exact candidate head.

All implementation steps 1–7 are complete. Step 8 is the final review gate.

## 2. First safety gate: issued-but-unconsumed closure

If `authorize_commit()` issued a permit but `consume_permit()` denied before `permit.consumed` became true, the canonical broker did not cross the mutation boundary. Layer 5 can retire that issued authority immediately as proved `NO_EFFECT` instead of waiting for permit expiry.

The closure contract is:

- owned by `CommitGateway`, not by Layer 4;
- valid only for the exact gateway-issued, still-unconsumed permit;
- authority-reducing, so it does not require current kill/policy/epoch liveness;
- never restores or reuses the already-SPENT approval;
- for REQUIRED/fenced effects, durable `NO_EFFECT` is appended before the in-memory attempt is terminalized;
- if the durable close fails, the permit remains issued and the attempt remains `RESERVED`, preserving conservative recovery and permitting an explicit closure retry;
- a consumed permit can never be rewritten as `NO_EFFECT`.

## 3. Completed supported live surface

The supported Dispatcher routes every remote mutation through M5 scoped authority:

- engagement: `bookmark_post`, `like_post`;
- text content: `post_text`, `reply_post`, `quote_post`;
- media content: `post_photo`, `reply_photo`, `quote_photo`, `post_multi_image`, `reply_multi_image`, `quote_multi_image`;
- destructive state change: `delete_post`.

`compose_post` remains outside the M5 mutation set only because it is a deliberate dry-run/no-effect shell. The transitional `WriteKernel` receives an inert no-mutation broker, and any future unmigrated WRITE capability is denied before composition rather than falling back to legacy mutation authority.

Post-submit confirmation is deliberately evidence-bound. Plain posts require a stable pre-submit direct-status baseline, one stable unique new direct status, direct timestamp ownership, exact status ID and canonical actor-owned URL, approved actor, and exact direct text. Reply and quote paths additionally prove their approved target/thread or quoted-target lineage. Media confirmation proves the same content lineage plus rendered attachment count; it does not claim source-byte equivalence after platform transcoding. Delete confirmation requires target-permalink-bound explicit deletion evidence; absence, generic errors, wrong-page state, or ambiguity remain UNKNOWN.

## 4. Migration invariants

Layer 5 preserves all of the following:

1. Every supported remote mutation crosses exactly one Commit Gateway.
2. Capability-facing code never receives a raw M5 broker/browser mutation surface.
3. Preparation authority and canonical effect authority remain distinct.
4. Permit mint and consumption remain at the final broker commit seam, after safe staging/probing.
5. A denied commit hook cannot fall through to a canonical browser mutation.
6. Clean precommit abandonment releases/terminalizes the attempt deliberately and consumes the bounded retry budget only according to the approval model.
7. After authority crosses, result truth is evidence-based: confirmed evidence records `EFFECT_CONFIRMED`; ambiguity records `EFFECT_UNKNOWN`.
8. Unknown external outcomes are never automatically retried.
9. Broker and CommitGateway in the supported live path share the exact same `KillSwitch` instance.
10. Legacy live mutation routes are unavailable from the supported Dispatcher/capability path after migration.

## 5. Deliberately deferred

- RecoveryGuard startup/replay blocking implementation (Layer 6);
- invocation-journal role retirement (Layer 7);
- hostile-Python sandboxing;
- cross-process browser serialization;
- distributed exactly-once semantics.
