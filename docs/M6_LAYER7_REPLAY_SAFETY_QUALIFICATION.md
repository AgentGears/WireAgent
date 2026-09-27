# M6 Layer 7 — Evidence-Driven Replay-Safety Qualification

```text
Status: ADVERSARIAL FINDING RECONCILED — FINAL EXACT-HEAD REVALIDATION PENDING
Baseline: 7b1bf7ee044ba653371e44d4abac7f7254b2c14d
Scope: M6_DESIGN.md §18, invariant 33, build-order layer 7
Candidate: like / unlike
```

This document is the maintainer-first findings register and qualification record
for M6 Layer 7. The first-pass findings below were frozen before implementation
and before the distinct adversarial second pass. The qualification question is
intentionally narrower than “does the UI look idempotent?”:

> Does concrete broker and platform evidence justify changing `like` / `unlike`
> from `ReplaySemantics.UNKNOWN` + `DurabilityPolicy.REQUIRED` to
> `SAFE_STATE_SET` + `BEST_EFFORT`?

The answer must remain **no promotion** unless the evidence establishes the
stronger replay property required by `M6_DESIGN.md` §18. Failed qualification is
retained evidence, not unfinished work.

## Frozen requirement

The normative matrix requires concrete broker behavior across at least:

```text
unliked + like    -> exactly liked
liked   + like    -> zero mutation
liked   + unlike  -> exactly unliked
unliked + unlike  -> zero mutation
both selectors present
neither selector present
hydration/state transition
stale selector
read/navigation coordination
```

A higher-level pre-state check is explicitly insufficient. Promotion also
requires evidence that replay creates no additional meaningful external effect;
public-engagement residuals such as notifications, counters, callbacks, or
analytics cannot be ruled out from DOM state alone.

## Review surface

The first pass reopened:

- `docs/M6_DESIGN.md` §18, invariant 33, build order, and completion criteria;
- `docs/M5_LAYER4_DESIGN.md` replay and exact-commit boundary;
- `src/webwire/safety/effect_policy.py` classification and durability derivation;
- `src/webwire/m5_write_broker.py` exact target-state and directional click seam;
- `src/webwire/m5_leased_write_broker.py` same-browser write serialization;
- `src/webwire/safety/m5_authority_factory.py` supported live construction path;
- `src/webwire/capabilities/like.py` legacy/high-level pre-state behavior;
- `tests/test_effect_policy.py`, `tests/test_m5_write_broker.py`,
  `tests/test_m5_leased_write_broker.py`, and Layer-4 exact-head regressions.

## Maintainer-first findings register

### L7-F01 — contradictory like controls are currently misclassified — production defect

`M5WriteBroker._target_state()` probes the target article for the clear selector
(`unlike`) before the set selector (`like`). If both are simultaneously present,
it returns `liked` rather than treating the DOM as contradictory.

That is not sufficient for a state-set qualification. “Both selectors present”
is an explicit §18 case, and contradictory controls must not become positive
state authority. The supported live broker must return `unknown`, so both like
and unlike fail before commit authority is crossed.

Severity: safety correctness. Confidence: high.

### L7-F02 — neither-selector state already fails closed — clean area

When neither directional control is present, the inherited reader returns
`unknown`. `click_like()` and `click_unlike()` refuse `unknown` before the commit
gate and perform no target click. Layer 7 locks that behavior into the
qualification matrix.

Severity: none. Confidence: high.

### L7-F03 — already-satisfied directional operations are zero-mutation — qualified locally

The concrete broker returns before the commit gate when the target is already in
the requested state. Existing behavior and the Layer-7 regressions establish
zero gate / zero mutation for `liked + like` and `unliked + unlike` at the
supported broker boundary. This is local directional DOM idempotence, not proof
of external replay safety.

Severity: none. Confidence: high.

### L7-F04 — stale post-probe controls require exact-seam revalidation — evidence gap

The requested directional control must still be the only authoritative visible
direction after commit authority crosses. Layer 7 therefore re-resolves the
approved article and revalidates the requested direction immediately at the
click seam. If the state changed, became contradictory, or the requested control
became stale, the broker fails after authority crossing without clicking the
opposite direction and without page-global fallback.

Severity before implementation: evidence gap. Confidence after direct regression: high.

### L7-F05 — engagement must share browser read/write coordination — evidence gap

`M5LeasedWriteBroker` wraps `click_like()` / `click_unlike()` in `_one_shot()`.
The shared per-browser async lock serializes M5 one-shot operations, and an
active content-composer owner blocks engagement navigation. Layer 7 adds an
engagement-specific assertion so this requirement is not inferred only from
bookmark coverage.

Severity before implementation: evidence gap. Confidence after direct regression: high.

### L7-F06 — repository evidence cannot establish absence of residual public-engagement effects — claim blocker

Repository evidence proves target-scoped directional DOM behavior. It does not
observe or falsify additional platform effects caused by repeated public
engagement: notification emission/suppression, engagement-event callbacks,
analytics/event ingestion, counter transitions, or other server-side effects.
No repository artifact provides a concrete broker/platform trace proving that a
replayed like/unlike which converges to the same boolean state produces no
additional meaningful external effect.

Because `BEST_EFFORT` is a positive replay-safety claim, absence of this evidence
blocks promotion. The policy must remain `UNKNOWN` / `REQUIRED` unless later live
platform evidence crosses that claim boundary.

Severity: qualification blocker, not a runtime defect. Confidence: high.

### L7-F07 — policy truth is already conservative and protected by regression — clean area

`effect_policy.py` deliberately classifies like/unlike as `UNKNOWN`, and
`tests/test_effect_policy.py` explicitly prevents DOM idempotence from being
mistaken for `BEST_EFFORT`. Layer 7 does not weaken those assertions merely to
complete the milestone.

Severity: none. Confidence: high.

## Implemented qualification surface

The candidate intentionally changes broker evidence, not replay policy.

`M6ReplayQualifiedWriteBroker` is now the broker returned by the supported live
write factory while remaining an `M5LeasedWriteBroker` subtype. It preserves the
M5 lease/scoped-authority contract and adds only the Layer-7 like/unlike
qualification behavior:

1. target-state reads are restricted to the approved target article;
2. like/unlike direction is based on visible, connected controls rather than raw
   selector presence;
3. both visible controls, neither visible control, hidden/disconnected controls,
   and unresolved hydration remain `unknown`;
4. hydration is given a small bounded observation window rather than one
   unqualified snapshot;
5. already-satisfied direction returns before permit/gate/mutation;
6. after commit authority crosses, the exact target article and requested
   direction are revalidated in the same JavaScript evaluation that performs the
   click;
7. if the requested control is stale/hidden, the opposite direction appears, or
   both are visible, no click is issued;
8. no opposite-control or page-global fallback exists;
9. an owned content composer blocks engagement before navigation through the
   existing M5 browser lease;
10. the default effect policy remains `UNKNOWN` / `REQUIRED`.

The Layer-7 regression matrix directly covers:

```text
not_liked + like       -> one gate, one like, final liked
liked + like           -> zero gate, zero mutation
liked + unlike         -> one gate, one unlike, final not_liked
not_liked + unlike     -> zero gate, zero mutation
both controls          -> bounded unknown, zero gate, zero mutation
neither control        -> bounded unknown, zero gate, zero mutation
hidden like/unlike     -> bounded unknown, zero gate, zero mutation
hydration transition   -> bounded polling, conclusive direction only
post-gate state change -> no wrong-direction/fallback click
post-gate contradiction-> no click
post-gate hidden/stale -> no click
active content owner   -> engagement blocked before navigation
live factory           -> qualified broker is the supported write path
policy                 -> UNKNOWN / REQUIRED remains exact registry truth
```

## Initial exact-candidate CI evidence

Candidate `09cc7aeca038e902cbb5c51eeab83b9350831abe` passed CI #463 before the
adversarial second pass:

```text
Ubuntu 24.04 / CPython 3.11.16
  952 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 85 source files

Ubuntu 24.04 / CPython 3.12.14
  952 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 85 source files

Windows Server 2025 / CPython 3.11 and 3.12
  existing Layer-6 focused durability qualification remained green
```

That green head was then treated as frozen input to the distinct adversarial
second pass rather than as automatic approval.

## Distinct adversarial second pass

No general Codex correctness-review integration was exposed in the available
plugin surface; the available Codex-specific integration was a security scanner,
not the independent general correctness review required by this layer. The
frozen-design fallback was therefore used: a separate adversarial review against
the exact green candidate.

The pass re-opened the generated DOM expressions, supported factory path,
post-authority mutation seam, lease coordination, policy registry, tests, and the
claim ceiling.

### L7-R01 — hidden/stale selector presence was still treated as authority — corrected

The first candidate fixed the “both selectors” contradiction but still used raw
`querySelector()` presence. A hidden or disconnected stale like/unlike element
could therefore be treated as authoritative state, and the post-gate expression
could call `.click()` on a hidden requested control.

The correction requires `isConnected` plus non-empty `getClientRects()` for both
state observation and post-authority click authority. Hidden/disconnected
controls now resolve to `unknown` before the gate or `stale` after it. New tests
cover hidden pre-state and hidden post-gate transitions and assert that the
production JavaScript contains the visibility checks.

Review-driven correction commits:

```text
a48b0c05e9d465103851d35972aaf107602e3f46  production visibility hardening
41de024896298fa50ba592ef1f97c03cb92797a0  hidden/stale regression matrix
```

No replay-policy promotion was introduced, and no second production defect was
found in this adversarial pass.

## Qualification decision

Layer 7 separates two different claims.

The **concrete broker directional-state mechanics are qualified** by the local
implementation/regression evidence: target-scoped directional behavior,
already-satisfied no-op behavior, contradictory/missing/hidden fail-closed
behavior, bounded hydration, exact-seam revalidation, no opposite fallback, and
browser lease coordination.

The **external replay-safety promotion is not qualified**. The available evidence
does not establish absence of residual public-engagement effects outside the DOM
boolean state. Therefore the correct completed Layer-7 policy result is deliberate
conservative retention:

```text
like    -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
unlike  -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
```

This is a completed qualification result, not a deferred assumption. A later
policy promotion would require new concrete external evidence and a new review;
it must not be inferred from this layer's broker convergence tests.

## Claim ceiling

Layer 7 supports only the following bounded statement:

> The supported live WireAgent broker implements and regression-tests
> directional, target-scoped like/unlike convergence behavior under the tested
> DOM ambiguity, hydration, staleness, and same-browser coordination cases.

It does **not** establish:

- absence of notification or engagement-event side effects;
- absence of analytics/callback/counter effects on the service;
- HTTP/API-level idempotency of the underlying platform;
- replay safety after arbitrary service-side state transitions;
- permission to downgrade like/unlike durability to `BEST_EFFORT`;
- a general replay-safety result for follow/unfollow/repost/unrepost;
- distributed exactly-once semantics.

## Final gate

Because L7-R01 changed production code and tests after CI #463, the complete
post-review head, including this close-out record, must pass the full exact-head
CI matrix before review can be closed and the PR pinned for merge. Any new
finding after that run requires reconciliation and another exact-head run.
