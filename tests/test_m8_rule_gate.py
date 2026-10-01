"""M8 layer 2 acceptance tests — the kernel rule gate (frozen spec section 6
as amended: single mint seam, explicit approver attribution, gate dominates
tokens) plus the lineage tests T22/T23/T24.

The kernel tests use a fake capability + adapter that records the approver
value it received — the critical Layer-2 rule is that approval source is an
explicit trusted value passed to the execution adapter, never "skip token
validation."
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from webwire.config import WebWireConfig
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    KillSwitch,
    TokenBucket,
    WriteIntent,
)
from webwire.safety.models import PolicyVerdict
from webwire.safety.user_rules import (
    RuleDecision,
    RuleSelector,
    RuleStore,
    UserRule,
)
from webwire.safety.write_kernel import WriteKernel

URL = "https://x.com/infaag/status/2102451358305771541"
NOW = 1000.0


def _rule(decision, *, action_types=None, rule_id=None, ttl=3600.0):
    return UserRule(
        rule_id=rule_id or f"r-{decision.value}",
        decision=decision,
        created_at=NOW,
        expires_at=NOW + ttl,
        selector=RuleSelector(action_types=action_types),
        source_text="test",
    )


def _store(tmp_path: Path, rules: list[UserRule]) -> RuleStore:
    RuleStore(tmp_path / "rules.json", clock=lambda: NOW).save(rules)
    return RuleStore(tmp_path / "rules.json", clock=lambda: NOW)


class _RecordingCap:
    """Like-capability whose adapter records the approver it received."""

    name = "like_post"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.require("like")
        return WriteIntent(
            action_type="like", target_type="post", target_id=input.get("post_id", "1"),
            risk_meta=meta, compensation=comp, actor_identity=actor_identity or "infaag",
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> Any:
        from webwire.safety.write_kernel import PreviewResult
        return PreviewResult(summary=f"Will like {intent.target_id}")

    async def execute(self, intent: WriteIntent, broker: Any) -> Any:
        from webwire.envelope import ok_result
        return ok_result(data={"liked": intent.target_id, "approver_seen": "legacy-shape"})

    async def execute_with_approver(
        self, intent: WriteIntent, broker: Any, approver: str
    ) -> Any:
        from webwire.envelope import ok_result
        self.approver_seen = approver  # type: ignore[attr-defined]
        return ok_result(data={"liked": intent.target_id, "approver": approver})

    async def verify(self, intent: WriteIntent, broker: Any) -> Any:
        from webwire.envelope import ok_result
        return ok_result(data={"verified": True})


def _kernel(tmp_path: Path, rule_store: Optional[RuleStore]) -> tuple[WriteKernel, _RecordingCap]:
    from webwire.config import WebWireConfig
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    kernel = WriteKernel(
        kill,
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        rule_store=rule_store,
    )
    cap = _RecordingCap()
    return kernel, cap


async def _p1(kernel: WriteKernel, cap: _RecordingCap, token: str = ""):
    payload = {"post_id": "1"}
    if token:
        payload["confirmation_token"] = token
    return await kernel.execute(cap, object(), payload)


# ---------------------------------------------------------------------------
# T13/T14: NEVER dominates — before and despite tokens
# ---------------------------------------------------------------------------

async def test_T13_never_denies_no_token(tmp_path: Path) -> None:
    kernel, cap = _kernel(tmp_path, _store(tmp_path, [
        _rule(RuleDecision.NEVER, action_types=frozenset({"like"}), rule_id="ban-like"),
    ]))
    r = await _p1(kernel, cap)
    assert r.data["policy"]["blocked_by"] == "user_rule"
    assert r.data["data"]["rule_id"] == "ban-like"
    assert "rule_approved" not in r.data["trace"]["stages"]
    assert not hasattr(cap, "approver_seen")


async def test_T14_never_beats_valid_old_human_token(tmp_path: Path) -> None:
    """The gate runs before token validation on the token-bearing invocation:
    a NEVER installed after a human token was issued still denies."""
    kernel, cap = _kernel(tmp_path, None)  # no rules at token-issue time
    r1 = await _p1(kernel, cap)
    assert r1.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    token = r1.data["data"]["confirmation_token"]

    # NEVER arrives between phase 1 and phase 2.
    kernel._rule_store = _store(tmp_path, [
        _rule(RuleDecision.NEVER, action_types=frozenset({"like"})),
    ])
    r2 = await _p1(kernel, cap, token=token)
    assert r2.data["policy"]["blocked_by"] == "user_rule"
    assert not hasattr(cap, "approver_seen"), "no execution past a standing ban"


# ---------------------------------------------------------------------------
# T15/T18: ASK path (matched and ceiling-downgraded)
# ---------------------------------------------------------------------------

async def test_T15_ask_uses_confirmation_and_cites_rule(tmp_path: Path) -> None:
    kernel, cap = _kernel(tmp_path, _store(tmp_path, [
        _rule(RuleDecision.ASK, action_types=frozenset({"like"}), rule_id="ask-like"),
    ]))
    r1 = await _p1(kernel, cap)
    assert r1.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    assert r1.data["data"]["rule_gate"]["matched_rule_id"] == "ask-like"

    token = r1.data["data"]["confirmation_token"]
    r2 = await _p1(kernel, cap, token=token)
    assert r2.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert cap.approver_seen == "human"


async def test_T18_above_ceiling_allow_downgrades_to_ask(tmp_path: Path) -> None:
    kernel, cap = _kernel(tmp_path, _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"like"}), rule_id="allow-like"),
    ]))
    # like is PUBLIC_REVERSIBLE_ENGAGEMENT — below the ceiling. To hit the
    # ceiling we point the rule at a public-content action instead.
    kernel._rule_store = _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"post"}), rule_id="allow-post"),
    ])
    # Reuse the recording cap with a post intent via a tiny subclass.
    class _PostCap(_RecordingCap):
        name = "post_text"

        def compose(self, input, actor_identity):  # type: ignore[no-untyped-def]
            meta, comp = DEFAULT_REGISTRY.require("post")
            return WriteIntent(
                action_type="post", target_type="none", target_id="none",
                risk_meta=meta, compensation=comp,
                actor_identity=actor_identity or "infaag",
            )

    cap2 = _PostCap()
    kernel2, _ = _kernel(tmp_path, kernel._rule_store)
    kernel2._rule_store = kernel._rule_store
    r1 = await kernel2.execute(cap2, object(), {"text": "hi"})
    assert r1.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    rg = r1.data["data"]["rule_gate"]
    assert rg["matched_rule_id"] == "allow-post"
    assert rg["ceiling_downgraded"] is True
    assert not hasattr(cap2, "approver_seen"), "above-ceiling allow must not execute"


# ---------------------------------------------------------------------------
# T16/T19: no-match and store-failure equivalence with pre-M8
# ---------------------------------------------------------------------------

async def test_T16_no_match_identical_to_pre_m8(tmp_path: Path) -> None:
    """T16's written contract: byte-for-byte identity. A no-match store must
    not alter the response beyond the three volatile fields every token mint
    carries (the random confirmation_token, its wall-clock created_at and
    expires_at) — those are normalized in BOTH envelopes, then the entire
    response is serialized and compared. Any other drift, including an
    unauthorized rule_gate key anywhere, fails the comparison."""
    rules = _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"})),
    ])
    pre, cap_pre = _kernel(tmp_path, None)
    post, cap_post = _kernel(tmp_path, rules)
    r_a = await _p1(pre, cap_pre)
    r_b = await _p1(post, cap_post)
    assert r_a.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    assert r_b.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("confirmation_token", "expires_at", "created_at"):
                    out[k] = f"<{k}>"
                else:
                    out[k] = scrub(v)
            return out
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    a = json.dumps(scrub(r_a.data), sort_keys=True)
    b = json.dumps(scrub(r_b.data), sort_keys=True)
    assert a == b, "no-match must leave the response byte-for-byte pre-M8"
    assert "rule_gate" not in r_b.data["trace"]
    assert "rule_gate" not in r_b.data["data"]


async def test_T19_corrupt_store_falls_back_to_human_confirmation(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_text("{broken", encoding="utf-8")
    kernel, cap = _kernel(tmp_path, RuleStore(p, clock=lambda: NOW))
    r = await _p1(kernel, cap)
    assert r.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    assert not hasattr(cap, "approver_seen")


# ---------------------------------------------------------------------------
# T17: below-ceiling ALLOW — no human token, rule attribution end-to-end
# ---------------------------------------------------------------------------

async def test_T17_allow_grants_without_token_and_attributes_rule(tmp_path: Path) -> None:
    kernel, cap = _kernel(tmp_path, _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"like"}), rule_id="allow-like"),
    ]))
    r = await _p1(kernel, cap)  # ONE invocation — no confirmation phase
    assert r.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert "rule_approved" in r.data["trace"]["stages"]
    assert cap.approver_seen == "rule:allow-like"
    assert r.data["trace"]["approver"] == "rule:allow-like"
    assert "confirmation_token" not in r.data["data"]


async def test_allow_requires_approver_capable_adapter(tmp_path: Path) -> None:
    """A capability without the attribution seam must not execute on standing
    approval (denied: approver_unsupported_adapter)."""

    class _LegacyCap(_RecordingCap):
        execute_with_approver = None  # type: ignore[assignment]

    kernel, _ = _kernel(tmp_path, _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"like"})),
    ]))
    legacy = _LegacyCap()
    r = await kernel.execute(legacy, object(), {"post_id": "1"})
    assert r.data["policy"]["blocked_by"] == "approver_unsupported_adapter"
    assert not hasattr(legacy, "approver_seen")


# ---------------------------------------------------------------------------
# T20/T21: rule approval under policy drift / epoch bump (existing denials)
# ---------------------------------------------------------------------------




def test_T23_pre_m8_ledger_rows_still_parse(tmp_path: Path) -> None:
    """A record without the approver field (pre-M8 shape) parses; approver
    reads None; M8-shaped rows round-trip the value."""
    from webwire.safety.effect_ledger import EffectLedgerRecord, EffectState

    rec = EffectLedgerRecord(
        effect_id="e1", semantic_key="k1", state=EffectState.RESERVED,
        action_type="post", intent_hash="h" * 32, policy_binding="p" * 32,
        actor_id="u", target_type="none", target_id="none",
    )
    payload = json.loads(rec.to_jsonl())
    payload.pop("approver", None)  # pre-M8 row: no approver key at all
    legacy = EffectLedgerRecord.from_dict(payload)
    assert legacy.approver is None

    payload["approver"] = "rule:allow-post"
    m8 = EffectLedgerRecord.from_dict(payload)
    assert m8.approver == "rule:allow-post"


# ---------------------------------------------------------------------------
# F-14 replacements: REAL lifecycle qualification (PR #22 review)
# ---------------------------------------------------------------------------

class _FakeLikeWriteBroker:
    """Minimal write broker matching the commit-gate protocol the scoped
    authority expects (keyword _commit_gate, same shape as the executor
    test fakes)."""

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ):
        from webwire.envelope import ok_result
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"liked": post_url})


class _FakeEvidenceReader:
    """Answers the executor's post-mutation state read with the exact state
    its verification expects, so record_confirmed() terminalizes."""

    async def read_bookmark_state(self, post_url: str):  # type: ignore[no-untyped-def]
        from webwire.envelope import ok_result
        return ok_result(data={"bookmark_state": "bookmarked"})

    async def read_like_state(self, post_url: str):  # type: ignore[no-untyped-def]
        from webwire.envelope import ok_result
        return ok_result(data={"like_state": "liked"})


def _live_runtime_and_ledger(tmp_path: Path):
    """A real M5 runtime + gateway + file-backed effect ledger, so lifecycle
    tests inspect actual durable records rather than source text. The scoped
    authority broker needs a write broker; the tests never invoke the
    browser through it (apply() is faked at the receipt level where needed),
    so a None write broker satisfies construction."""
    from webwire.safety.commit_gateway import CommitGateway
    from webwire.safety.effect_ledger import EffectLedger
    from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
    from webwire.safety.execution_models import AuthorizationEpoch
    from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
    from webwire.safety.scoped_authority import ScopedAuthorityBroker

    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    scoped = ScopedAuthorityBroker(
        write_broker=_FakeLikeWriteBroker(), commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        grants=None,  # default pruning store
        policies=DEFAULT_EFFECT_POLICIES,
    )
    return runtime, gateway, ledger, epoch


def _engagement_intent(action: str = "like", target: str = "42") -> WriteIntent:
    meta, comp = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action, target_type="post", target_id=target,
        risk_meta=meta, compensation=comp, actor_identity="infaag",
    )


def test_T20_real_policy_drift_denies_rule_grant_at_commit(tmp_path: Path) -> None:
    """Frozen T20 scenario, via the ACTUAL commit path: the rule-ALLOW grant
    is minted under policy P; the LIVE registry shared by the runtime, the
    scoped authority, and the CommitGateway drifts BEFORE commit;
    session.scope_effect() re-freezes the binding against that live registry
    and the grant's minted binding no longer matches — denied with
    policy_mismatch. No durable fact is written."""
    from webwire.safety.effect_policy import (
        DEFAULT_EFFECT_POLICIES,
        EffectPolicy,
        EffectVerb,
        ReplaySemantics,
    )
    from webwire.safety.scoped_authority import ScopedAuthorityDenied

    runtime, _gateway, ledger, _epoch = _live_runtime_and_ledger(tmp_path)
    session = runtime.issue(_engagement_intent(), approver="rule:allow-like")
    assert session.grant.approver == "rule:allow-like"

    # The registry instance the runtime, scoped broker, and gateway all hold.
    # Drift 'like' by changing its replay semantics — the binding hash
    # changes while allowed_effects stays exactly {SET_LIKE}, so the denial
    # is the binding mismatch, not effect_scope_not_exact. Restore in
    # finally so the shared singleton is left untouched.
    original = DEFAULT_EFFECT_POLICIES.require("like")
    drifted = EffectPolicy.derive(
        action_type="like",
        risk_tier=DEFAULT_REGISTRY.require("like")[0].derive_tier(),
        allowed_effects=frozenset({EffectVerb.SET_LIKE}),
        replay_semantics=ReplaySemantics.SAFE_STATE_SET,
    )
    assert drifted.binding_hash() != original.binding_hash(), "drift must change the binding"

    try:
        DEFAULT_EFFECT_POLICIES.register(drifted)
        with pytest.raises(ScopedAuthorityDenied) as exc:
            session.scope_effect()  # the actual commit path begins here
        assert exc.value.reason == "policy_mismatch", exc.value.reason
        assert ledger.read_records() == [], "denial must precede any durable fact"
    finally:
        DEFAULT_EFFECT_POLICIES.register(original)


def test_T21_rule_grant_dies_on_epoch_advancement_at_commit(tmp_path: Path) -> None:
    """Frozen T21 scenario, via the ACTUAL commit path: the rule-derived
    approval is minted through the real runtime; the gateway's
    AuthorizationEpoch advances; the commit attempt then denies with
    epoch_mismatch — and no re-validation can succeed, so a second commit
    attempt denies identically. No durable fact is written."""
    from webwire.safety.scoped_authority import ScopedAuthorityDenied

    runtime, _gateway, ledger, epoch = _live_runtime_and_ledger(tmp_path)
    session = runtime.issue(_engagement_intent(), approver="rule:allow-like")
    minted_epoch = session.grant.authorization_epoch
    assert session.grant.claimed_by == session.attempt.attempt_id  # claimed at mint

    epoch.bump()  # the gateway's AuthorizationEpoch — after mint, before commit
    assert epoch.current != minted_epoch

    with pytest.raises(ScopedAuthorityDenied) as first:
        session.scope_effect()  # the actual commit path begins here
    assert first.value.reason == "epoch_mismatch", first.value.reason
    # validate_live terminally REVOKES an epoch-invalidated grant: the
    # approval itself is dead, so no re-validation can ever succeed —
    # every later attempt denies at the ACTIVE-state check instead.
    assert session.grant.state.value == "revoked", session.grant.state.value
    with pytest.raises(ScopedAuthorityDenied) as second:
        session.scope_effect()  # no re-validation can succeed
    assert second.value.reason == "grant_not_active", second.value.reason
    assert ledger.read_records() == [], "denial must precede any durable fact"


def test_T22_reservation_and_terminal_share_approver_in_durable_ledger(tmp_path: Path) -> None:
    """Full lifecycle: a rule-granted session scopes an effect through the
    REAL gateway; every durable record in the file ledger carries the
    identical approver, and the fenced like policy (ReplaySemantics.UNKNOWN)
    must durably record BOTH the reservation and the terminal confirmation.
    No inspect.getsource."""
    import asyncio

    runtime, gateway, ledger, epoch = _live_runtime_and_ledger(tmp_path)

    async def main():
        session = runtime.issue(_engagement_intent(), approver="rule:allow-like")
        receipt = session.scope_effect()
        await receipt.authority.apply()
        session.record_confirmed(evidence={"verified": True})

    asyncio.run(main())

    rows = ledger.read_records()
    assert rows, "the lifecycle must durably record the effect"
    approvers = {r.approver for r in rows}
    assert approvers == {"rule:allow-like"}, (
        f"one effect lifecycle, one approver; saw {approvers} across {len(rows)} records"
    )
    states = {r.state.value for r in rows}
    assert "RESERVED" in states, f"fenced policy must durably reserve: {states}"
    assert "EFFECT_CONFIRMED" in states, states


async def test_T24_human_execution_attributed_human_through_real_gateway(tmp_path: Path) -> None:
    """Frozen T24 scenario: a HUMAN-CONFIRMED execution. The kernel issues a
    confirmation token (phase 1), the second invocation consumes it (phase
    2), and execution then crosses the real adapter → runtime → gateway
    permit → file ledger. Every durable record says 'human', never
    rule-derived; the fenced reservation and terminal confirmation are both
    present."""
    from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter
    from webwire.safety.m5_effect_executor import M5EffectExecutor

    runtime, _gateway, ledger, _epoch = _live_runtime_and_ledger(tmp_path)
    executor = M5EffectExecutor(runtime=runtime, evidence_reader=_FakeEvidenceReader())
    adapter = M5EngagementCapabilityAdapter(_RecordingCap(), executor)
    kernel, _ = _kernel(tmp_path, None)  # no rules — the human path

    r1 = await kernel.execute(adapter, object(), {"post_id": "1"})
    assert r1.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    token = r1.data["data"]["confirmation_token"]

    r2 = await kernel.execute(
        adapter, object(), {"post_id": "1", "confirmation_token": token}
    )
    assert r2.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert r2.data["trace"]["approver"] == "human"

    rows = ledger.read_records()
    assert rows, "human-confirmed execution must reach the durable ledger"
    assert all(r.approver == "human" for r in rows), [r.approver for r in rows]
    states = {r.state.value for r in rows}
    assert "RESERVED" in states and "EFFECT_CONFIRMED" in states, states


def test_ledger_rejects_approver_lineage_mismatch(tmp_path: Path) -> None:
    """F-11 regression: RESERVED approver=rule:a then EFFECT_CONFIRMED
    approver=rule:b for the same effect is a lineage contradiction."""
    from webwire.safety.effect_ledger import EffectLedger, EffectLedgerRecord, EffectState

    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    base = dict(
        effect_id="e1", semantic_key="k1", action_type="like",
        intent_hash="h" * 32, policy_binding="p" * 32,
        actor_id="u", target_type="post", target_id="1",
    )
    ledger.append_durable(EffectLedgerRecord(
        state=EffectState.RESERVED, approver="rule:a", **base))
    with pytest.raises(Exception, match="(?i)lineage|approver|contradict"):
        ledger.append_durable(EffectLedgerRecord(
            state=EffectState.EFFECT_CONFIRMED, approver="rule:b", **base))


def test_ledger_changed_approver_retry_is_a_different_fact(tmp_path: Path) -> None:
    """F-11 second consequence: an ambiguous append retried with a DIFFERENT
    approver is a different fact, not 'the same fact'."""
    from webwire.safety.effect_ledger import EffectLedger, EffectLedgerRecord, EffectState

    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    base = dict(
        effect_id="e2", semantic_key="k2", action_type="like",
        intent_hash="h" * 32, policy_binding="p" * 32,
        actor_id="u", target_type="post", target_id="2",
    )
    r1 = EffectLedgerRecord(state=EffectState.RESERVED, approver="rule:a", **base)
    r2 = EffectLedgerRecord(state=EffectState.RESERVED, approver="rule:b", **base)
    ledger.append_durable(r1)
    assert not ledger._same_fact(r1, r2), (
        "a changed-approver retry must not be treated as the same fact"
    )


def test_grant_approver_reassignment_rejected(tmp_path: Path) -> None:
    """F-10 regression: approver is sealed after mint."""
    from webwire.safety.execution_models import ApprovalGrantStore, GrantStateError

    store = ApprovalGrantStore(clock=lambda: NOW)
    grant = store.mint(
        intent_hash="a" * 32, actor_id="u", action_type="like",
        target_type="post", target_id="1", policy_binding="b" * 64,
        authorization_epoch=0, approver="rule:allow-like",
    )
    with pytest.raises(GrantStateError):
        grant.approver = "rule:different-rule"


def test_invalid_approver_vocabulary_rejected_everywhere(tmp_path: Path) -> None:
    """F-12/F-16 regression: the canonical validator rejects bad values at
    the validator, the mint seam, and the ledger parse. The rule-id suffix
    obeys the ONE shared layer-1 invariant (any non-empty string), so a
    spaced id such as 'rule:allow likes' is VALID attribution; blank
    suffixes and foreign prefixes are not."""
    from webwire.safety.effect_ledger import EffectLedgerCorruptError, EffectLedgerRecord
    from webwire.safety.execution_models import ApprovalGrantStore, validate_approver

    for bad in ("", "rule:", "rule:   ", "machine", "Human"):
        with pytest.raises(ValueError):
            validate_approver(bad)
    validate_approver("human")
    validate_approver("rule:x")
    validate_approver("rule:allow likes")  # F-16: legal layer-1 rule id
    validate_approver(None, allow_none=True)

    store = ApprovalGrantStore(clock=lambda: NOW)
    with pytest.raises(ValueError):
        store.mint(
            intent_hash="a" * 32, actor_id="u", action_type="like",
            target_type="post", target_id="1", policy_binding="b" * 64,
            authorization_epoch=0, approver="",
        )

    # Canonical UPPERCASE state: the state parses cleanly, so the APPROVER
    # vocabulary is provably what rejected the row. (A lowercase state would
    # fail at EffectState() first and prove nothing about the approver.)
    payload = {
        "effect_id": "e3", "semantic_key": "k3", "state": "RESERVED",
        "action_type": "like", "intent_hash": "h" * 32, "policy_binding": "p" * 32,
        "approver": "machine",
    }
    with pytest.raises(EffectLedgerCorruptError, match="attribution vocabulary"):
        EffectLedgerRecord.from_dict(payload)


async def test_F16_legal_spaced_rule_id_flows_to_durable_approver(tmp_path: Path) -> None:
    """F-16 regression: 'allow likes' is a LEGAL layer-1 rule id (the frozen
    UserRule contract accepts any non-empty string). With one shared
    invariant it now rides the entire lineage — store round-trip, kernel
    rule-ALLOW attribution, runtime mint, durable ledger — with no layer
    re-imposing a stricter id contract mid-stream and failing the mint."""
    from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter
    from webwire.safety.m5_effect_executor import M5EffectExecutor

    spaced = UserRule.create(
        selector=RuleSelector(action_types=frozenset({"like"})),
        decision=RuleDecision.ALLOW, rule_id="allow likes",
    )
    store = _store(tmp_path, [spaced])
    assert [r.rule_id for r in store.load()] == ["allow likes"]

    runtime, _gateway, ledger, _epoch = _live_runtime_and_ledger(tmp_path)
    executor = M5EffectExecutor(runtime=runtime, evidence_reader=_FakeEvidenceReader())
    adapter = M5EngagementCapabilityAdapter(_RecordingCap(), executor)
    kernel, _ = _kernel(tmp_path, store)

    r = await kernel.execute(adapter, object(), {"post_id": "9"})
    assert r.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert r.data["trace"]["approver"] == "rule:allow likes"

    rows = ledger.read_records()
    assert rows
    assert all(rec.approver == "rule:allow likes" for rec in rows), [
        rec.approver for rec in rows
    ]


async def test_F09_live_dispatcher_installs_rule_store_and_ban_reaches_kernel(
    tmp_path: Path,
) -> None:
    """F-09 regression: normal Dispatcher construction wires the RuleStore —
    a persisted NEVER denies through the ordinary authority path."""
    import webwire.dispatcher as disp_mod
    from webwire.config import WebWireConfig

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    # The Dispatcher's store reads the REAL clock, so the rule must use the
    # real-time default TTL (a NOW-anchored fixture rule would arrive expired).
    RuleStore(cfg.rules_path()).save([
        UserRule.create(
            selector=RuleSelector(action_types=frozenset({"like"})),
            decision=RuleDecision.NEVER, rule_id="live-ban",
        ),
    ])
    d = disp_mod.Dispatcher(cfg)
    assert d._write_kernel._rule_store is not None, "Dispatcher must install the store"
    cap = _RecordingCap()
    r = await d._write_kernel.execute(cap, object(), {"post_id": "1"})
    assert r.data["policy"]["blocked_by"] == "user_rule"
    assert r.data["data"]["rule_id"] == "live-ban"
    assert not hasattr(cap, "approver_seen")
