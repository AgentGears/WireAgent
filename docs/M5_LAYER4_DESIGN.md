# M5 Layer 4 — Scoped Authorities

**Status:** maintainer first-pass design frozen for implementation  
**Base:** Layer 3 squash `f8ffdeb`  
**Codex:** not consulted for this design pass

This document resolves the Layer-4 question deliberately left open by
`docs/M5_DESIGN.md`: how a single-use `EffectPermit` coexists with slow,
reversible composer preparation while preventing capabilities from receiving the
full `WriteBroker` mutation surface.

The goal of Layer 4 is **least execution authority**, not live-path migration.
Layer 5 will wire concrete capabilities through these objects.

---

## 1. Forcing function

Today the legacy `WriteKernel` constructs one concrete `WriteBroker` and passes
that whole object to every write capability for execute/verify. `ports.py`
contains useful Protocol shapes, but structural typing alone is not runtime
authority: a capability holding the concrete object can still call every method
on it.

Layer 4 must make the executable object itself narrower.

Required shape:

```text
approved frozen intent
        |
        +--> PreparationAuthority   # bounded staging only
        |
        +--> CommitGateway -> EffectPermit
                     |
                     +--> EffectAuthority  # one canonical effect only
```

A capability must not receive raw `WriteBroker`, raw browser primitives, or an
authority for the inverse effect merely because the concrete broker implements
them.

---

## 2. Maintainer first-pass findings

### L4-F1 / P1 — preparation and commit effects are conflated

Current `EffectPolicy.allowed_effects` puts these in one set for content actions:

- `OPEN_COMPOSER`
- `FILL_COMPOSER`
- `ATTACH_MEDIA`
- `SUBMIT_CONTENT`

But `EffectPermit` is intentionally single-use. One permit cannot correctly
serve all four sequential operations:

- minting before slow composer/media work starts its TTL too early;
- consuming on an early staging operation leaves no fresh one-shot authority for
  submit;
- reusing one consumed permit across staging and submit violates Layer 3.

**Resolution:** split policy vocabulary into:

- `PreparationVerb`: bounded staging operations that cannot publish the
  canonical effect;
- `EffectVerb`: permit-consumable canonical external effects.

For post/reply/quote the canonical permit effect is only `SUBMIT_CONTENT`.
Preparation policy is bound into the policy hash, but preparation does not
consume an `EffectPermit`.

This is not a claim that staging has zero observable remote behavior. Media
upload, draft state, and composer interaction can touch the service. M5's durable
at-most-once/reconciliation contract applies to the **canonical semantic effect**;
staging is separately constrained by active approval, exact intent binding,
kill checks in the concrete broker, and cleanup. Layer 4 does not claim durable
exactly-once staging.

### L4-F2 / P1 — method-only scoping still permits argument smuggling

A wrapper exposing only `click_bookmark(post_url)` is insufficient if the caller
can supply a URL unrelated to the approved target while the gateway validates
the permit's original `target_id`.

The same problem exists for:

- post/reply/quote text;
- reply/quote target URL + target id;
- delete target URL + id;
- media source paths.

**Resolution:** every scoped object owns one private deep-copied approved intent
binding. Mutating browser arguments are derived from or checked against that
binding. Canonical effect methods take no caller-selected target/payload
arguments. Preparation methods either take no argument or reject anything that
does not exactly match the frozen approved value.

### L4-F3 / P1 — consuming at authority-method entry is still too early

Several semantic broker methods perform staging before the actual mutation:

- bookmark reads current state before clicking;
- like navigates/hydrates before clicking;
- delete navigates, opens menus, waits for the confirmation sheet, then clicks
  confirm;
- content staging can take far longer than a permit TTL.

If an authority consumes the permit before delegating to those methods, the
Commit Gateway is not the exact mutation boundary and may create false
uncertainty.

**Resolution:** the concrete broker mutation method accepts a private commit
hook supplied only by the scoped effect authority. The broker invokes that hook
**immediately before the actual mutating click/confirm**, after all preparatory
state checks. The hook calls `CommitGateway.consume_permit(...)`.

Legacy callers omit the hook, preserving Layer-4 non-migration behavior. Layer 5
removes raw-broker exposure from live capability execution.

### L4-F4 / P1 — inverse authority must not ride along with a concrete broker

`WriteBroker` implements both bookmark/remove-bookmark and like/unlike. Granting
one direction must not expose the other direction.

**Resolution:** one effect authority represents exactly one `EffectVerb`.
Examples:

```text
SetBookmarkAuthority       -> SET_BOOKMARK only
ClearBookmarkAuthority     -> CLEAR_BOOKMARK only
SetLikeAuthority           -> SET_LIKE only
ClearLikeAuthority         -> CLEAR_LIKE only
DeletePostAuthority        -> DELETE_POST only
SubmitContentAuthority     -> SUBMIT_CONTENT only
```

The factory rejects a permit whose effect set is not exactly the expected
singleton for the requested action binding.

### L4-F5 / P1 — preparation must not exist without live human approval

A staging wrapper created from `WriteIntent` alone could be used before the
human approval gate.

**Resolution:** preparation authority creation requires:

- exact `ApprovalGrant`;
- exact claimed `EffectAttempt`;
- grant still `ACTIVE`;
- attempt still `PREPARING`;
- claim held by that attempt;
- current policy binding;
- current authorization epoch;
- exact intent/actor binding.

Every mutating preparation operation revalidates those conditions before
calling the concrete broker. Once `authorize_commit()` spends the grant, the
preparation authority becomes unusable automatically.

### L4-F6 / P2 — permit payload binding must survive caller mutation

`WriteIntent` remains a mutable legacy dataclass. Its `intent_hash()` covers the
payload, but a scoped wrapper cannot retain the caller-owned object and re-read
it later.

**Resolution:** authority construction deep-copies once, computes the binding
from that copy, extracts immutable typed values, and never re-reads the original
intent. Effect authority construction additionally requires exact equality with
permit action/actor/target/intent hash/policy binding.

### L4-F7 / P2 — abort cleanup must remain possible when authority is revoked

`close_composer()` is a reducing/cleanup operation, but the current concrete
broker applies the same kill guard as effect-producing mutations. A trip during
media preparation can therefore block cleanup and leave a partial composer open.

**Resolution:** cleanup authority is one-way state reduction and is allowed even
when normal preparation liveness has failed. `WriteBroker.close_composer()` must
not be blocked by the mutation kill guard. It still exposes no submit or other
external effect.

### L4-F8 / P2 — outcome lifecycle must remain gateway-owned

Layer 4 must not invent a second effect state machine. Scoped effect authority
may consume the permit at the concrete boundary, but durable
`EFFECT_CONFIRMED` / `EFFECT_UNKNOWN` recording continues through
`CommitGateway` with the canonical attempt object. Layer 5 owns orchestration of
verification and which terminal outcome is justified by evidence.

---

## 3. Policy model after Layer 4

```text
EffectPolicy
├── risk_tier
├── preparation_effects: frozenset[PreparationVerb]
├── allowed_effects: frozenset[EffectVerb]      # permit-consumable only
├── replay_semantics
├── durability
└── binding_hash()                              # includes both authority sets
```

Initial content policy:

```text
post / reply / quote
  preparation_effects = {
      OPEN_COMPOSER,
      FILL_COMPOSER,
      ATTACH_MEDIA,
  }
  allowed_effects = {SUBMIT_CONTENT}
```

Bookmark, like, delete and future one-step semantic mutations have no
preparation verbs unless evidence later requires them.

`EffectPermit.allowed_effects` therefore remains a set for Layer-3 compatibility,
but Layer-4 effect-authority construction requires the set to be a singleton.
A broader permit is rejected rather than translated into a broader authority.

---

## 4. Scoped authority broker

Layer 4 introduces a process-local adapter around the concrete broker:

```text
ScopedAuthorityBroker(
    write_broker,
    commit_gateway,
    authorization_epoch,
    effect_policies,
)

prepare(grant, attempt, intent)
  -> ContentPreparationAuthority

authorize(permit, attempt, intent)
  -> SetBookmarkAuthority
   | ClearBookmarkAuthority
   | SetLikeAuthority
   | ClearLikeAuthority
   | DeletePostAuthority
   | SubmitContentAuthority
```

It never returns the underlying `WriteBroker`.

Same-process Python reflection can still reach private implementation state if
code is actively hostile. Layer 4 is least-authority engineering against normal
capability code, not a sandbox. Untrusted plugins still require process/OS
isolation and no raw browser handle.

---

## 5. Preparation authority

`ContentPreparationAuthority` is bound to one active approval claim and one
frozen intent. Its public mutation surface is limited to staging operations
required by the current content family.

Expected operations include bounded equivalents of:

- open/fill plain composer from approved text;
- open/fill approved reply target;
- open/fill approved quote target;
- attach only approved canonical media paths, in approved order;
- read composer text;
- verify attachment readiness;
- count attachments;
- cleanup/close composer.

It exposes **no submit method**.

Media staging validates the frozen manifest path/order and rechecks the approved
SHA-256 before delegating upload. A caller cannot swap a path while retaining the
approved permit hash.

Preparation authority becomes invalid for new staging when the grant is no
longer ACTIVE, the claim is lost, policy identity changes, or authorization
epoch changes. Cleanup remains allowed because it only reduces staged state.

---

## 6. Effect authority and exact commit hook

Effect authority owns:

- exact `EffectPermit` object;
- exact canonical `EffectAttempt` object;
- frozen intent binding;
- one expected `EffectVerb`;
- gateway reference;
- private concrete-broker reference.

The public semantic method is parameterless with respect to approved target and
payload. Example:

```text
SetBookmarkAuthority.apply()
  -> broker.click_bookmark(bound_post_url, _commit_gate=consume_exact_permit)

SubmitContentAuthority.submit()
  -> broker.click_submit(_commit_gate=consume_exact_permit)

DeletePostAuthority.delete()
  -> broker.delete_post(bound_url, bound_id,
                        _commit_gate=consume_exact_permit)
```

The concrete broker calls `_commit_gate` only immediately before the actual
mutation. The hook consumes the permit with the frozen intent hash, actor,
target, policy binding, and exact effect verb.

If the gateway denies the hook, the broker must not click.

If the process crashes after permit consumption and before an outcome record,
Layer 3's durable reservation/unknown semantics remain authoritative. Layer 4
adds no exactly-once claim.

---

## 7. Required concrete-broker seams

Layer 4 may add an optional private keyword-only commit hook to the existing
semantic methods. Legacy callers that omit it retain current behavior until
Layer 5 migration.

Required exact hook points:

- bookmark/remove-bookmark: after state-first no-op/unknown resolution,
  immediately before the directional click;
- like/unlike: immediately before the directional click; if implementation is
  made state-first as part of this seam, replay policy is **not** automatically
  upgraded without separate broker-level evidence;
- submit: after final broker kill guard, immediately before tweet-button click;
- delete: first poll until confirmation control exists without clicking, then
  run the hook, then perform one confirmation click. Do not consume authority
  before menu/navigation staging.

---

## 8. Layer boundary

Layer 4 **does not**:

- route `WriteKernel` through M5;
- change dispatcher live behavior;
- migrate capabilities;
- add RecoveryGuard;
- make the invocation journal audit-only;
- claim cross-process isolation;
- claim exactly-once effects.

Those remain Layers 5–7.

---

## 9. Acceptance tests

Layer 4 is not complete until adversarial tests prove at least:

1. bookmark authority cannot call clear-bookmark, like, delete, or submit;
2. clear-bookmark authority cannot set bookmark;
3. like/unlike directions are independent authorities;
4. delete authority cannot change target URL/id after construction;
5. content submit authority has no composer/media preparation methods;
6. preparation authority has no submit/delete/engagement methods;
7. preparation creation fails without the exact active claimed grant;
8. preparation revalidation fails after grant expiry/revocation/spend;
9. policy-binding drift invalidates preparation;
10. caller mutation after authority construction does not alter target/text/media;
11. media path substitution is rejected before upload;
12. media order cannot be rearranged by the caller;
13. media digest change is rejected before upload;
14. effect authority rejects permit/frozen-intent mismatch;
15. effect authority rejects a non-singleton/broadened permit effect set;
16. target A permit cannot navigate/mutate target B through authority arguments;
17. gateway permit is unconsumed during bookmark state probe and consumed only at
    the actual click hook;
18. gateway permit is unconsumed during delete navigation/menu staging and
    consumed only immediately before confirmation click;
19. expired/revoked/killed/policy-stale permit denial prevents the broker click;
20. permit reuse remains rejected;
21. submit permit receives its full Layer-3 TTL until the final submit boundary;
22. cleanup remains callable after kill/revocation and cannot publish content;
23. scoped objects expose no public raw `WriteBroker`, raw browser, or generic
    click/fill primitive;
24. existing legacy live-path tests remain unchanged/green because Layer 5 has
    not migrated execution yet.

---

## 10. Implementation order

1. Split preparation verbs from permit-consumable effect verbs in EffectPolicy;
   update binding tests and normative M5 text.
2. Add exact private commit-hook seams to concrete broker mutations with no
   behavior change when the hook is absent.
3. Implement frozen intent binding + `ScopedAuthorityBroker`.
4. Implement `ContentPreparationAuthority`.
5. Implement exact effect-authority classes.
6. Add adversarial Layer-4 tests.
7. Run full pytest/Ruff/mypy on Python 3.11/3.12 CI.
8. Complete exhaustive maintainer first-pass review of the implementation and
   freeze its findings in the PR.
9. Only then request independent Codex review.

---

## 11. Frozen Layer-4 invariants

1. Capabilities do not need the concrete mutation broker to express an approved
   semantic effect.
2. Preparation and canonical commit authority are distinct.
3. Preparation requires an ACTIVE, exact, claimed human approval.
4. Permit mint spends approval; spent approval disables further preparation.
5. Policy binding covers both staging and canonical effect authority.
6. One effect authority exposes exactly one permit-consumable `EffectVerb`.
7. Effect target/payload arguments are frozen from the approved intent, not
   selected by the capability at execution time.
8. Permit consumption happens at the concrete mutation seam, not at wrapper
   construction or method entry.
9. A denied commit hook cannot fall through to the browser click.
10. Cleanup is reducing authority and remains available when normal preparation
    authority is revoked.
11. Layer 4 does not own effect truth; durable terminal outcomes remain the
    CommitGateway/EffectLedger state machine.
12. Same-process scoped authority is an engineering boundary, not hostile-code
    isolation.
