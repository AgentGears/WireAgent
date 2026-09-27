# M6 Layer 7 — Evidence-Driven Replay-Safety Qualification

```text
Status: FIRST-PASS FINDINGS FROZEN — IMPLEMENTATION / QUALIFICATION PENDING
Baseline: 7b1bf7ee044ba653371e44d4abac7f7254b2c14d
Scope: M6_DESIGN.md §18, invariant 33, build-order layer 7
Candidate: like / unlike
```

This document is the maintainer-first findings register for M6 Layer 7. It was
frozen before implementation and before any independent/adversarial second
review. The qualification question is intentionally narrower than “does the UI
look idempotent?”:

> Does concrete broker and platform evidence justify changing `like` / `unlike`
> from `ReplaySemantics.UNKNOWN` + `DurabilityPolicy.REQUIRED` to
> `SAFE_STATE_SET` + `BEST_EFFORT`?

The default answer remains **no promotion** unless the evidence establishes the
stronger replay property required by `M6_DESIGN.md` §18. Failed qualification is
retained evidence, not treated as unfinished work.

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
state authority. The broker should return `unknown`, so both like and unlike
fail before commit authority is crossed.

Severity: safety correctness. Confidence: high.

### L7-F02 — neither-selector state already fails closed — clean area

When neither directional control is present, `_target_state()` returns `unknown`.
`click_like()` and `click_unlike()` refuse `unknown` before the commit gate and
perform no target click. This behavior should be locked into the Layer-7 matrix.

Severity: none. Confidence: high.

### L7-F03 — already-satisfied directional operations are zero-mutation — qualified locally

The concrete broker returns before the commit gate when the target is already in
the requested state. Existing tests cover like and unlike retry/no-op behavior on
the approved target. This establishes local directional DOM idempotence for the
modeled broker boundary, not external replay safety.

Severity: none. Confidence: high.

### L7-F04 — stale post-probe controls fail conservatively, but need direct Layer-7 evidence

The broker re-resolves the exact directional control inside the exact approved
article after the commit gate. It has no page-global fallback. If hydration or a
concurrent state change makes that selector stale, the click seam can fail after
a permit crossed, which is conservatively terminalized as unknown by the M5
execution path rather than redirected to another control.

Layer 7 needs an explicit regression that changes directional state between the
pre-state probe and the target click and proves no opposite/fallback mutation is
performed.

Severity: evidence gap. Confidence: medium-high pending direct regression.

### L7-F05 — one-shot engagement is serialized with browser write ownership — locally supported

`M5LeasedWriteBroker` wraps `click_like()` / `click_unlike()` in `_one_shot()`.
The shared per-browser async lock serializes M5 one-shot operations, and an
active content-composer owner blocks engagement navigation. Existing lease tests
exercise the generic one-shot block through bookmark; Layer 7 should add an
engagement-specific assertion so the required read/navigation coordination is
not inferred indirectly.

Severity: evidence gap. Confidence: high.

### L7-F06 — current evidence cannot establish absence of residual public-engagement effects — claim blocker

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
mistaken for `BEST_EFFORT`. Layer 7 must not weaken those assertions merely to
complete the milestone.

Severity: none. Confidence: high.

## First-pass decision before implementation

The layer will **not** begin by changing replay policy. The candidate changes are
limited to evidence-driven broker hardening, a complete qualification matrix,
and close-out documentation. A policy promotion is permitted only if the
resulting evidence independently establishes the stronger external replay
property. Based on the evidence currently present in the repository, the
expected close-out is deliberate conservative retention:

```text
like    -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
unlike  -> ReplaySemantics.UNKNOWN / DurabilityPolicy.REQUIRED
```

## Candidate implementation surface

1. Harden target-state classification so `like` + `unlike` simultaneously
   present is `unknown` rather than `liked`.
2. Add Layer-7 broker regressions for all §18 directional/ambiguous/stale cases.
3. Add engagement-specific browser-lease/read-navigation coordination coverage.
4. Retain policy assertions proving no unsupported downgrade occurred.
5. Record the evidence ceiling and final qualification result in this document,
   README, and state tracking.

No live mutation test is authorized merely to manufacture a policy promotion.
If external platform evidence is unavailable, the valid Layer-7 result is a
failed promotion with conservative policy preserved.
