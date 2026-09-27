# M6 Layer 7 — Evidence-Driven Replay-Safety Qualification

```text
Status: QUALIFICATION RESULT FROZEN — CONSERVATIVE RETENTION; EXACT-HEAD CLOSE-OUT GATE APPLIES
Baseline: 7b1bf7ee044ba653371e44d4abac7f7254b2c14d
Scope: M6_DESIGN.md §18, invariant 33, build-order layer 7
Candidate: like / unlike
Result: broker/evidence mechanics qualified; replay-policy promotion not qualified
```

This document is the maintainer-first findings register and qualification record
for M6 Layer 7. The first-pass findings below were frozen before implementation
and before the distinct adversarial second pass. The qualification question is
intentionally narrower than “does the UI look idempotent?”:

> Does concrete broker and platform evidence justify changing `like` / `unlike`
> from `ReplaySemantics.UNKNOWN` + `DurabilityPolicy.REQUIRED` to
> `SAFE_STATE_SET` + `BEST_EFFORT`?

The answer is **no promotion** unless evidence establishes the stronger replay
property required by `M6_DESIGN.md` §18. Failed promotion is retained evidence,
not unfinished work.

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

The maintainer-first pass and later adversarial pass reopened:

- `docs/M6_DESIGN.md` §18, invariant 33, build order, and completion criteria;
- `docs/M5_LAYER4_DESIGN.md` replay and exact-commit boundary;
- `src/webwire/safety/effect_policy.py` classification and durability derivation;
- `src/webwire/m5_write_broker.py` exact target-state and directional click seam;
- `src/webwire/m5_leased_write_broker.py` same-browser write serialization;
- `src/webwire/safety/m5_authority_factory.py` supported live construction path;
- `src/webwire/safety/m5_live_runtime.py` supported live execution/evidence wiring;
- `src/webwire/safety/m5_evidence_reader.py` terminal like-state evidence path;
- `src/webwire/safety/m5_effect_executor.py` post-permit terminalization behavior;
- `src/webwire/capabilities/like.py` legacy/high-level pre-state behavior;
- effect-policy, broker, live-runtime, exact-head, and Layer-7 regressions.

## Maintainer-first findings register

### L7-F01 — contradictory like controls were misclassified — production defect

`M5WriteBroker._target_state()` probes the target article for the clear selector
(`unlike`) before the set selector (`like`). If both are simultaneously present,
it returns `liked` rather than treating the DOM as contradictory.

That is insufficient for state-set qualification. “Both selectors present” is
an explicit §18 case, and contradictory controls must not become positive state
authority. The supported live broker therefore classifies contradiction as
`unknown`, so both directions fail before commit authority is crossed.

Severity: safety correctness. Confidence: high.

### L7-F02 — neither-selector state already failed closed — clean area

When neither directional control is present, the inherited reader returns
`unknown`. `click_like()` and `click_unlike()` refuse `unknown` before the commit
gate and perform no target click. Layer 7 locks that behavior into the
qualification matrix.

Severity: none. Confidence: high.

### L7-F03 — already-satisfied directional operations are zero-mutation — locally qualified

The concrete broker returns before the commit gate when the target is already in
the requested state. Existing behavior and Layer-7 regressions establish zero
gate / zero mutation for `liked + like` and `unliked + unlike` at the supported
broker boundary. This is local directional DOM idempotence, not proof of external
replay safety.

Severity: none. Confidence: high.

### L7-F04 — stale post-probe controls required exact-seam revalidation — evidence gap

The requested directional control must still be the only authoritative direction
after commit authority crosses. Layer 7 re-resolves the approved article and
revalidates the requested direction immediately at the click seam. If state
changed, became contradictory, or the requested control became stale, the broker
fails after authority crossing without clicking an opposite/fallback control.

Severity before implementation: evidence gap. Confidence after direct regression: high.

### L7-F05 — engagement must share browser read/write coordination — evidence gap

`M5LeasedWriteBroker` wraps `click_like()` / `click_unlike()` in `_one_shot()`.
The shared per-browser async lock serializes one-shot operations, and an active
content-composer owner blocks engagement navigation. Layer 7 adds engagement and
terminal-evidence assertions so this property is direct evidence rather than an
inference from bookmark coverage.

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

### L7-F07 — policy truth was already conservative and regression-protected — clean area

`effect_policy.py` deliberately classifies like/unlike as `UNKNOWN`, and the
policy tests explicitly prevent DOM idempotence from being mistaken for
`BEST_EFFORT`. Layer 7 does not weaken those assertions merely to complete the
milestone.

Severity: none. Confidence: high.

## Implemented qualification surface

The candidate changes broker and evidence mechanics, not replay policy.

`M6ReplayQualifiedWriteBroker` is returned by the supported live write factory
while remaining an `M5LeasedWriteBroker` subtype. It preserves the M5 lease and
scoped-authority contract and strengthens only the Layer-7 like/unlike boundary:

1. target-state reads remain restricted to the approved target article;
2. controls must be directly owned by that article, not a nested/quoted article;
3. an authoritative control must be connected, have layout geometry, be
   CSS-visible/interactable under the bounded predicate, and not be
   `aria-hidden`, `aria-disabled`, or native `:disabled`;
4. exactly one requested-direction control and zero opposite controls are
   required; duplicates or contradictions are ambiguous;
5. neither direction, hidden/disabled/disconnected controls, nested controls,
   duplicate controls, and unresolved hydration remain `unknown`;
6. hydration has a bounded observation window rather than one unqualified
   snapshot;
7. already-satisfied direction returns before permit/gate/mutation;
8. after commit authority crosses, target identity and both directions are
   revalidated in the same synchronous JavaScript evaluation that performs the
   click;
9. stale/hidden/disabled/nested/duplicate/contradictory post-gate state issues no
   click;
10. no opposite-control or page-global fallback exists;
11. an owned content composer blocks engagement before navigation through the
   existing M5 browser lease.

`M6ReplayQualifiedEvidenceReader` closes the terminal-evidence side of the same
boundary. The supported live stack uses it for consumed-like verification, and
its `read_like_state()` calls the qualified broker reader under the same
`_one_shot()` browser lease. Therefore execution cannot reject an ambiguous
selector arrangement while terminal evidence later accepts that same arrangement
through the weaker Layer-5 base reader.

The supported live construction path is now pinned by tests to both the
Layer-7-qualified write broker and the Layer-7-qualified evidence reader.

## Regression matrix

The Layer-7 matrix directly covers:

```text
not_liked + like          -> one gate, one like, final liked
liked + like              -> zero gate, zero mutation
liked + unlike            -> one gate, one unlike, final not_liked
not_liked + unlike        -> zero gate, zero mutation
both controls             -> bounded unknown, zero gate, zero mutation
neither control           -> bounded unknown, zero gate, zero mutation
hidden/disconnected       -> bounded unknown, zero gate, zero mutation
CSS-hidden/noninteractive -> bounded unknown, zero gate, zero mutation
disabled controls         -> bounded unknown, zero gate, zero mutation
nested quoted controls    -> bounded unknown, zero gate, zero mutation
duplicate direction       -> bounded unknown, zero gate, zero mutation
hydration transition      -> bounded polling, conclusive direction only
post-gate state change    -> no wrong-direction/fallback click
post-gate contradiction   -> no click
post-gate stale/hidden    -> no click
post-gate disabled/nested -> no click
post-gate duplicate       -> no click
active content owner      -> engagement blocked before navigation
terminal like evidence    -> same qualified state semantics + browser lease
live stack construction   -> qualified broker + qualified evidence reader
policy                    -> UNKNOWN / REQUIRED remains registry truth
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

That green head was treated as frozen input to the distinct adversarial second
pass rather than automatic approval.

## Distinct adversarial second pass

No general Codex correctness-review integration was exposed in the available
plugin surface; the available Codex-specific integration was a security scanner,
not the independent general correctness review required by this layer. The
frozen-design fallback was therefore used: a separate adversarial review after
the first exact-green candidate.

### L7-R01 — hidden/disconnected raw selector presence remained authority — corrected

The first candidate fixed the “both selectors” contradiction but still used raw
selector presence. A hidden or disconnected stale like/unlike element could be
treated as state authority, and the post-gate expression could attempt a click.
The correction added connected/layout visibility qualification before either
state observation or mutation authority.

### L7-R02 — nested/duplicate controls could inherit accidental DOM-order authority — corrected

A control inside a nested quoted article can be a descendant of the approved
outer article, and raw `querySelector()` ordering can arbitrarily choose among
multiple same-direction controls. The correction filters controls to
`closest('article') === approved_article`, uses `querySelectorAll()`, and requires
an exact one-versus-zero directional cardinality before state or click authority.

### L7-R03 — CSS-hidden/disabled controls were still too weakly classified — corrected

Layout rectangles alone are not sufficient interaction authority. The control
predicate was strengthened to reject display/visibility/pointer-event hidden
states, `aria-hidden`, `aria-disabled`, and native `:disabled`. Pre-gate and
post-gate regressions cover those states and assert that the production
JavaScript carries those checks.

### L7-R04 — terminal evidence bypassed the qualified Layer-7 reader — corrected

The most important second-pass integration finding was downstream of mutation.
The supported live `M5EffectExecutor` verifies a consumed like permit through its
evidence reader. The prior `M5LeasedEvidenceReader.read_like_state()` explicitly
called the base `M5WriteBroker.read_like_state()`, so a mutation could be guarded
by Layer-7 semantics while terminal evidence still used weaker Layer-5 selector
semantics.

`M6ReplayQualifiedEvidenceReader` now preserves the actor-bound evidence surface
but routes terminal like-state verification through the exact qualified broker
reader under the shared one-shot browser lease. The live execution stack requires
the qualified write broker and installs the qualified evidence reader. Direct
regressions prove contradictory terminal evidence stays `unknown` and that an
owned composer blocks terminal evidence before navigation.

No replay-policy promotion was introduced by any correction.

## Post-adversarial implementation CI

Candidate `055130d15d54f9945a4f7c639d7138b79f19b795`, containing R01–R04
production fixes and the expanded qualification matrix, passed CI #473:

```text
Ubuntu 24.04 / CPython 3.11.16
  984 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 86 source files

Ubuntu 24.04 / CPython 3.12.14
  984 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 86 source files

Windows Server 2025 / windows-2025-vs2026
  CPython 3.11.9: 147 focused safety-ledger tests passed
  CPython 3.12: existing focused Layer-6 durability job passed
```

A later test-only construction invariant pins the supported live stack to the
qualified broker/evidence reader. Close-out documentation/state changes follow
that code candidate, so the final PR head still requires the normal exact-head
four-job CI gate before merge.

## Qualification decision

Layer 7 separates two claims.

The **supported broker/evidence directional-state mechanics are qualified** by
the implementation and regression evidence: exact target/article ownership,
directional convergence, already-satisfied no-op behavior,
contradictory/missing/hidden/disabled/nested/duplicate fail-closed behavior,
bounded hydration, exact-seam revalidation, no opposite fallback, same-browser
coordination, and equivalent terminal like-state evidence semantics.

The **external replay-safety promotion is not qualified**. The available
evidence does not establish absence of residual public-engagement effects outside
the DOM boolean state. Therefore the correct completed Layer-7 policy result is
deliberate conservative retention:

```text
like    -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
unlike  -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
```

This is a completed qualification result, not a deferred assumption. A later
policy promotion requires new concrete external evidence and a new review; it
must not be inferred from this layer's broker-convergence tests.

## Claim ceiling

Layer 7 supports only this bounded statement:

> The supported live WireAgent broker and terminal like-evidence path implement
> and regression-test directional, target-scoped like/unlike mechanics under the
> tested DOM ambiguity, hydration, staleness, ownership, and same-browser
> coordination cases.

It does **not** establish:

- absence of notification or engagement-event side effects;
- absence of analytics/callback/counter effects on the service;
- HTTP/API-level idempotency of the underlying platform;
- replay safety after arbitrary service-side state transitions;
- permission to downgrade like/unlike durability to `BEST_EFFORT`;
- a general replay-safety result for follow/unfollow/repost/unrepost;
- whole-browser or service-level exactly-once semantics.

## Close-out gate

The qualification result above is frozen. The eventual merge head must still
pass the complete exact-head CI matrix (Ubuntu Python 3.11/3.12 full suite +
Ruff + mypy, and Windows Python 3.11/3.12 durability qualification) after all
close-out documentation/state changes. The exact green head is then subject to
one final recorded adversarial review. Any new finding requires reconciliation
and another exact-head validation before pinned merge.
