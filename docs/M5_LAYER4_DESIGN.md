# M5 Layer 4 — Scoped Authorities

**Status:** implementation contract aligned to the maintainer-reviewed runtime  
**Base:** Layer 3 squash `f8ffdeb`  
**Scope:** least execution authority; live capability migration remains Layer 5

Layer 4 answers one question left open by `docs/M5_DESIGN.md`: how a capability
can prepare a browser interaction and execute one approved semantic effect
without ever receiving the full mutation broker.

The implemented answer is **delayed authority mint + provenance-bound staging +
per-browser M5 write ownership**. An effect handle is not an `EffectPermit`.
The permit is minted and consumed only when the concrete broker has reached the
last safe point immediately before the canonical mutating click.

---

## 1. Threat model and non-goals

Layer 4 is a same-process least-authority engineering boundary against accidental
or ordinary capability overreach. It is not a sandbox for hostile Python.
Reflection or deliberate access to private implementation state remains outside
the guarantee; untrusted plugins require process/OS isolation and no raw browser
handle.

Layer 4 does **not**:

- migrate the legacy `WriteKernel`, Dispatcher, or capabilities;
- add RecoveryGuard;
- retire the invocation journal's legacy role;
- claim distributed exactly-once delivery;
- make browser staging durable or exactly-once;
- provide cross-process browser serialization.

---

## 2. Authority model

Policy separates bounded staging from the one canonical external effect:

```text
EffectPolicy
├── risk_tier
├── preparation_effects: frozenset[PreparationVerb]
├── allowed_effects: frozenset[EffectVerb]
├── replay_semantics
├── durability
└── binding_hash()  # includes both preparation and canonical effect surfaces
```

`PreparationVerb` currently contains:

- `OPEN_COMPOSER`
- `FILL_COMPOSER`
- `ATTACH_MEDIA`

`EffectVerb` contains the permit-consumable semantic effects such as
`SET_BOOKMARK`, `CLEAR_LIKE`, `SUBMIT_CONTENT`, and `DELETE_POST`.

For post/reply/quote:

```text
preparation_effects = {OPEN_COMPOSER, FILL_COMPOSER, ATTACH_MEDIA}
allowed_effects     = {SUBMIT_CONTENT}
```

Layer 4 requires the canonical `allowed_effects` set to be exactly the expected
singleton before it creates an effect handle. A broadened policy does not become
a broader runtime object.

Replay classification remains evidence-based:

- bookmark/remove-bookmark: `SAFE_STATE_SET` → `BEST_EFFORT`;
- like/unlike: `UNKNOWN` → `REQUIRED` despite directional DOM idempotence,
  because replay may still have residual public-engagement effects;
- follow/unfollow/repost/unrepost: `UNKNOWN` → `REQUIRED` until implemented
  evidence proves a stronger contract;
- content create: `NON_IDEMPOTENT_CREATE` → `REQUIRED`;
- delete: target-delete semantics, but risk still requires fencing.

---

## 3. Runtime shape

The supported live path is:

```text
real SuperBrowser facade
        |
        v
WireAgent live-facade proxy
        |
        v
M5LeasedWriteBroker (contract v3)
        |
        v
ScopedAuthorityBroker
        |
        +--> PreparationAuthority        # ACTIVE claimed approval only
        |
        +--> AuthorizedEffect receipt
                  |
                  +--> narrow EffectAuthority  # capability-facing
                  |
                  +--> exact permit lineage    # trusted orchestrator only;
                                                 # None until commit seam
```

The supported construction API is in `webwire.safety.m5_authority_factory`:

- `build_live_m5_write_broker(super_browser, kill_switch)`;
- `build_live_scoped_authority_broker(write_broker, commit_gateway, ...)`.

The first function wraps the external SDK facade in one WireAgent-owned,
attribute-capable proxy per exact facade identity. Browser-lease state therefore
does not depend on the third-party SDK accepting arbitrary attributes. Repeated
live construction for the same facade shares the same proxy and lease state.

The second function accepts only the factory-built `M5LeasedWriteBroker`
contract v3. Direct/provenance-only brokers are rejected for the supported live
path. The generic `ScopedAuthorityBroker` remains available for isolated adapter
and unit-test doubles; Layer 5 must use the live factory.

---

## 4. Frozen intent and target binding

`WriteIntent` remains mutable legacy state, so Layer 4 deep-copies it once and
reduces it to an immutable typed binding. The binding owns:

- action type;
- target type and target id;
- actor identity;
- intent hash;
- policy binding;
- approved status URL / target post id;
- approved normalized text;
- ordered media path + SHA-256 manifest.

Targeted post actions require an HTTPS `x.com`/`twitter.com` status URL whose
numeric `/status/<id>` equals the approved target id. Mutating method arguments
are derived from or checked against this frozen binding; capability code cannot
select a different target after approval.

Navigation is not itself target authority. Engagement state reads and clicks
resolve the article that directly owns the approved post's timestamp link and
operate only inside that article. Nested quoted-post timestamps cannot make the
outer article authoritative.

---

## 5. Preparation authority

Preparation authority requires the exact ACTIVE approval, exact claimed attempt,
current policy binding, current authorization epoch, and frozen intent/actor
binding.

The logical content sequence is bounded:

```text
post:          fill/open approved composer -> approved media -> seal
reply/quote:   open approved target context -> fill text -> approved media -> seal
```

Media operations require the approved order/path and recompute the approved
SHA-256 before upload.

### 5.1 Async staging ownership

Preparation is a state machine, not a collection of independent coroutine
calls. Each attempt tracker has:

- a lock;
- one active staging token;
- a monotonically increasing generation;
- cleanup/effect in-flight latches;
- sealed state.

Only one staging operation can own the token at a time. Cleanup may invalidate
an in-flight operation by advancing the generation, but it does not free that
operation's token until the coroutine actually returns. A stale completion can
therefore never make the preparation ready again after cleanup.

Once a content effect handle is created, the tracker is sealed. No further
staging is accepted. Cleanup remains a reducing operation and may still be used
to abandon browser draft state.

Layer 5 must terminalize/release a **proved precommit NO_EFFECT** when a sealed
content flow is deliberately abandoned before any permit exists; otherwise the
approval can remain safely but unnecessarily claimed.

---

## 6. Browser-side provenance and the write lease

`M5WriteBroker` provides the base exact mutation seams.
`M5ScopedWriteBroker` adds browser-DOM provenance for content staging.
`M5LeasedWriteBroker` adds per-browser transient ownership and is the only
supported live broker contract.

### 6.1 Provenance binding

Content staging does not trust page-global selectors. It binds generated opaque
markers to the exact transient DOM objects created by the approved flow:

- baseline existing composer contexts before target-triggering clicks;
- bind exactly one new approved reply/quote/plain composer;
- scope textarea typing to that bound context;
- scope file input to the bound context/form;
- baseline existing media previews, then bind exactly one new approved preview;
- at submit, prove context kind/target, exact text, exact media count, approved
  media markers/readiness, and one submit control;
- bind that submit control before the commit gate and revalidate/click the same
  marked control after the gate.

Ambiguity, a missing baseline, stale context, multiple new candidates, or a
changed bound control fails closed.

Delete uses the same provenance principle: baseline pre-existing menus and
confirmation controls, trigger the approved target's caret, bind exactly one new
Delete menu/item, bind exactly one new confirmation, then cross authority and
click the same marked confirmation.

### 6.2 Per-browser lease

Every `M5LeasedWriteBroker` sharing the same supported live facade proxy shares
one `_BrowserWriteState`:

- one asyncio operation lock serializes M5 browser operations;
- one content owner token persists for the lifetime of a bound composer;
- other M5 one-shot effects are denied while content owns the browser;
- failed content starts release ownership only when no content provenance was
  established;
- cleanup releases the owner when cleanup succeeds;
- once submit crosses commit authority, the broker releases content ownership
  after that boundary operation returns, even if the final browser click becomes
  `UNKNOWN`.

This lease is process-local and applies only to M5 writers constructed from the
same exact facade identity. Layer 5 must not concurrently route legacy/read
navigation through the same browser while an M5 lease is active.

---

## 7. Delayed effect authority

Creating an effect authority does **not** mint an `EffectPermit` and does not
spend approval.

```text
scope_effect(...)
    -> AuthorizedEffect(
           authority=<narrow semantic handle>,
           attempt=<canonical attempt>,
           permit=None,
       )
```

The capability-facing authority exposes only one semantic method:

```text
SetBookmarkAuthority.apply()
ClearBookmarkAuthority.apply()
SetLikeAuthority.apply()
ClearLikeAuthority.apply()
SubmitContentAuthority.submit()
DeletePostAuthority.delete()
```

It has no caller-selected target/payload parameters, no generic click/fill
primitive, no raw broker/browser reference, and no outcome-recording API.

The trusted orchestrator retains `AuthorizedEffect`; after the final mutation
boundary is attempted, `receipt.permit` exposes the exact canonical permit for
Layer-5 outcome recording through `CommitGateway`.

Effect invocation itself is single-use/in-flight guarded. A second concurrent
invocation is rejected; once a permit exists the authority cannot be reused.

---

## 8. Exact commit boundary

The concrete broker performs all safe staging/probing first. Its private commit
hook then performs, synchronously:

```text
CommitGateway.authorize_commit(...)
    -> [REQUIRED only: durable RESERVED]
    -> atomic approval spend
    -> exact EffectPermit
CommitGateway.consume_permit(...)
    -> final policy/epoch/kill/TTL/binding checks
    -> single-use authority crossing
```

Only after the hook succeeds may the canonical mutating click execute.

Consequences:

- already-satisfied bookmark/like state-set: no permit, no spend, no mutation;
- target missing / unresolved state: no permit, no spend;
- delete navigation/menu/confirmation staging failure: no permit, no spend;
- content payload/provenance failure: no permit, no spend;
- submit/delete/engagement permit TTL starts at the final mutation seam rather
  than at slow staging/probe entry.

If the bound control changes **after** authority crosses but before the click can
be proven, the result is `UNKNOWN`, never a fallback click on another control.

### 8.1 Known Layer-3 follow-up: issued but denied before consumption

There is one conservative lifecycle debt outside Layer 4's least-authority
correctness. If `authorize_commit()` successfully issues a fenced permit and a
concurrent kill/epoch/policy/binding change makes `consume_permit()` deny before
`consumed=True`, the private gate returns failure and the broker does not click.
The canonical effect is therefore known absent, but current Layer 3 retains the
unused permit/reservation until expiry/pruning closes it to `NO_EFFECT`.

This cannot produce an unauthorized effect. A crash before that expiry can,
however, recover the raw `RESERVED` as `EFFECT_UNKNOWN`, causing conservative
unnecessary reconciliation. The correct fix belongs to `CommitGateway`: add an
explicit gateway-owned issued-but-unconsumed `NO_EFFECT` closure. Do not make
Layer 4 mutate ledger/attempt truth directly. This follow-up must land before
Layer-5 production migration.

---

## 9. Outcome ownership

Layer 4 does not declare effect truth.

After the boundary, Layer 5 uses the exact permit and canonical attempt retained
by `AuthorizedEffect` to call:

- `CommitGateway.record_effect_confirmed(...)`, or
- `CommitGateway.record_effect_unknown(...)`

based on evidence and verification.

A crash after a fenced permit crosses authority and before terminal outcome
persistence remains Layer 3's `RESERVED -> EFFECT_UNKNOWN` recovery case. No
exactly-once claim is made.

---

## 10. Acceptance contract

Layer 4 tests must prove at least:

1. live factory rejects a broker weaker than v3 and rejects directly constructed
   live brokers that bypass the WireAgent facade proxy;
2. a slotted/non-extensible SDK facade can be used without WireAgent attributes;
3. repeated live broker construction for one exact SDK facade shares one lease;
4. each effect authority exposes one direction/effect only;
5. raw broker/browser/generic mutation methods are absent from capability-facing
   authority;
6. caller mutation after scope construction cannot alter frozen target/text/media;
7. target-A authority cannot act on a decoy/first article B;
8. nested quoted-post timestamps cannot authorize the outer article;
9. already-satisfied state-set is zero gate / zero permit / zero mutation;
10. state probe/delete staging/content proof happens before permit mint;
11. permit mint/consume occurs immediately before the exact bound mutating
    control;
12. missing/changed/ambiguous target or provenance fails before the gate;
13. broadened policy effect sets are rejected;
14. preparation requires exact ACTIVE claimed approval and current policy/epoch;
15. preparation obeys context -> text -> media order and approved media digest;
16. overlapping async staging is serialized by an owned token;
17. cleanup invalidates stale staging completion without releasing its token
    early;
18. preparation is sealed before content effect use;
19. cleanup cannot race an active effect invocation and effect cannot start
    during cleanup;
20. effect invocation is single-use and concurrent duplicate invocation is
    rejected;
21. policy/kill drift before final mint denies without browser mutation;
22. submit proves exact bound context/text/media/button before crossing;
23. delete proves target-triggered menu/confirmation provenance before crossing;
24. browser lease blocks another M5 writer while a content context is owned;
25. failed staging with established provenance retains the lease until cleanup;
26. baseline/provenance failures fail closed before target-triggering action;
27. legacy WriteKernel/Dispatcher/capability behavior remains unchanged because
    live migration is not part of Layer 4.

---

## 11. Frozen Layer-4 invariants

1. Preparation authority and canonical commit authority are distinct.
2. Preparation requires one exact ACTIVE claimed approval.
3. Policy identity binds both staging and canonical effect surfaces.
4. Browser target/payload values are frozen from approved intent.
5. Capability-facing effect authority exposes exactly one canonical effect.
6. Effect-handle construction does not mint a permit or spend approval.
7. Slow navigation, state probing, delete staging, and content preparation occur
   before permit mint.
8. Permit mint and consumption occur only inside the concrete broker's final
   commit hook.
9. A denied commit hook cannot fall through to a canonical browser mutation.
10. Browser mutations after authority crossing use the same bound target/control
    that was proven before crossing; no page-global fallback is allowed.
11. Content DOM staging is provenance-bound and ambiguity fails closed.
12. One M5 browser facade has one process-local write lease in the supported live
    construction path.
13. Preparation/effect concurrency is token/latch controlled; stale async
    completion cannot restore authority.
14. Cleanup is reducing authority and remains available when new preparation is
    no longer authorized.
15. Layer 4 does not own effect truth; durable terminal outcome remains gateway /
    ledger authority.
16. Same-process least authority is not hostile-code isolation.

---

## 12. Layer-5 obligations

Before production migration, Layer 5 must:

- use `build_live_m5_write_broker()` and `build_live_scoped_authority_broker()`;
- pass only preparation/effect authority objects to capabilities, never raw M5
  broker/browser handles;
- terminalize clean precommit abandonment so claimed approvals are not stranded;
- apply the precommit retry budget by creating/releasing attempts deliberately,
  not by retrying one failed attempt forever;
- record confirmed/unknown outcomes synchronously from the exact receipt permit;
- coordinate read/verification navigation with the M5 browser lease;
- land the Layer-3 issued-but-unconsumed `NO_EFFECT` closure described in §8.1
  before production migration.
