"""M8 layer 4 acceptance tests — the card surface + rule lifecycle (frozen
spec section 8).

The properties under test:

- The card renders summary/warnings/matched-rule and holds the confirmation
  token INTERNALLY (never in to_dict, render_text, or repr).
- approve() replays the exact original payload with the held token (phase
  2); deny() invokes nothing and consumes the card; a card is single-use.
- Rule-ALLOW and NEVER outcomes return with NO card (nothing to approve).
- The rule lifecycle surface: list (fail-safe read, canonical descriptions,
  live TTL state) and TTL re-confirm (identity preserved, fenced update,
  corrupt store refuses, other rules untouched).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.envelope import ok_result
from webwire.m8_cards import (
    CardFlow,
    RuleCard,
    describe_rule,
    list_rules,
    reconfirm_rule,
)
from webwire.safety.user_rules import (
    DEFAULT_RULE_TTL_S,
    RuleDecision,
    RuleSelector,
    RuleStore,
    RuleStoreError,
    UserRule,
)

NOW = 2000.0


# ---------------------------------------------------------------------------
# Card flow over a fake invoke: shape, token custody, single-use
# ---------------------------------------------------------------------------


class _FakeInvoke:
    """Records every invocation and answers with scripted results."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, capability_name: str, payload: dict[str, Any]):
        self.calls.append((capability_name, dict(payload)))
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _phase1_result(
    *,
    verdict: str = "confirmation_required",
    token: str = "tok-1",
    matched_rule: dict | None = None,
    warnings: list[str] | None = None,
) -> Any:
    data: dict[str, Any] = {
        "preview": "Will like post 1",
        "target_url": "https://x.com/i/status/1",
        "current_state": "not_liked",
        "warnings": warnings or [],
        "confirmation_token": token,
        "intent_hash": "h" * 32,
        "capability_name": "like_post",
        "confirmation_epoch": 1,
        "expires_at": NOW + 300.0,
    }
    if matched_rule is not None:
        data["rule_gate"] = matched_rule
    return ok_result(data={
        "policy": {"verdict": verdict, "blocked_by": None},
        "data": data,
        "trace": {"stages": ["confirmation_required"]},
    })


def _allow_result() -> Any:
    return ok_result(data={
        "policy": {"verdict": "allow", "blocked_by": None},
        "data": {"liked": "1"},
        "trace": {"stages": ["confirmed", "execute_attempted"], "approver": "human"},
    })


async def test_card_holds_token_internally() -> None:
    invoke = _FakeInvoke([_phase1_result()])
    flow = CardFlow(invoke)
    _, card = await flow.begin("like_post", {"post_id": "1"})
    assert card is not None
    assert "tok-1" not in card.to_dict().__str__()
    assert "tok-1" not in card.render_text()
    assert "tok-1" not in repr(card)
    assert card.summary == "Will like post 1"
    assert card.matched_rule is None


async def test_approve_replays_original_payload_with_held_token() -> None:
    invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    flow = CardFlow(invoke)
    _, card = await flow.begin("like_post", {"post_id": "1", "dry_run": False})
    result = await card.approve(invoke)
    assert result.data["policy"]["verdict"] == "allow"
    # Phase 2 carried the EXACT original payload plus the held token.
    name, payload = invoke.calls[1]
    assert name == "like_post"
    assert payload == {
        "post_id": "1", "dry_run": False, "confirmation_token": "tok-1",
    }


async def test_deny_invokes_nothing_and_consumes_the_card() -> None:
    invoke = _FakeInvoke([_phase1_result()])
    flow = CardFlow(invoke)
    _, card = await flow.begin("like_post", {"post_id": "1"})
    denied = card.deny()
    assert denied.ok is False
    assert "denied by owner" in denied.error.message
    assert len(invoke.calls) == 1, "deny must not invoke anything"
    with pytest.raises(RuntimeError, match="already denied"):
        card.deny()
    with pytest.raises(RuntimeError, match="already denied"):
        await card.approve(invoke)


async def test_card_is_single_use_on_approve() -> None:
    invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    flow = CardFlow(invoke)
    _, card = await flow.begin("like_post", {"post_id": "1"})
    await card.approve(invoke)
    with pytest.raises(RuntimeError, match="already approved"):
        await card.approve(invoke)
    assert len(invoke.calls) == 2


async def test_rule_allow_and_never_return_no_card() -> None:
    allow = ok_result(data={
        "policy": {"verdict": "allow", "blocked_by": None},
        "data": {"liked": "1"},
        "trace": {"stages": ["rule_approved"], "approver": "rule:allow-like"},
    })
    result, card = await CardFlow(_FakeInvoke([allow])).begin(
        "like_post", {"post_id": "1"}
    )
    assert card is None
    assert result.data["policy"]["verdict"] == "allow"

    deny = ok_result(data={
        "policy": {"verdict": "deny", "blocked_by": "user_rule"},
        "data": {"rule_id": "ban-like", "decision": "never"},
        "trace": {"stages": ["denied:user_rule"]},
    })
    deny.ok = False
    result, card = await CardFlow(_FakeInvoke([deny])).begin(
        "like_post", {"post_id": "1"}
    )
    assert card is None
    assert result.data["policy"]["blocked_by"] == "user_rule"


async def test_ask_card_surfaces_matched_rule_and_warnings() -> None:
    matched = {"matched_rule_id": "ask-like", "ceiling_downgraded": False}
    invoke = _FakeInvoke([
        _phase1_result(matched_rule=matched, warnings=["post is from a stranger"]),
    ])
    flow = CardFlow(invoke)
    _, card = await flow.begin("like_post", {"post_id": "1"})
    assert card is not None
    assert card.matched_rule == {"matched_rule_id": "ask-like", "ceiling_downgraded": False}
    text = card.render_text()
    assert "ask-like" in text
    assert "post is from a stranger" in text
    assert "Will like post 1" in text


# ---------------------------------------------------------------------------
# Card flow over a REAL dispatcher (phase 1 → card → phase 2 executed)
# ---------------------------------------------------------------------------


async def test_card_drives_real_dispatcher_phase1_phase2(tmp_path: Path) -> None:
    """End to end through the ordinary authority path: a stubbed session and
    a fake M5 bookmark broker; the card's approve() consumes a REAL kernel
    token and the bookmark executes through the real executor + gateway."""
    from types import SimpleNamespace

    from webwire.config import WebWireConfig
    from webwire.dispatcher import Dispatcher
    from webwire.safety.commit_gateway import CommitGateway
    from webwire.safety.effect_ledger import EffectLedger
    from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
    from webwire.safety.execution_models import AuthorizationEpoch
    from webwire.safety.m5_effect_executor import M5EffectExecutor
    from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
    from webwire.safety.scoped_authority import ScopedAuthorityBroker
    from webwire.session import SessionManager

    class _StubSB:
        _page = None
        _controller = None

    class _StubSessionManager(SessionManager):
        def __init__(self, config: WebWireConfig) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True
            self.set_resolved_handle("@actor")

    class _FakeM5BookmarkBroker:
        def __init__(self) -> None:
            self.bookmark_clicks: list[str] = []

        async def click_bookmark(
            self,
            post_url: str,
            *,
            _commit_gate,  # type: ignore[no-untyped-def]
        ) -> Any:
            denied = _commit_gate()
            if denied is not None:
                return denied
            self.bookmark_clicks.append(post_url)
            return ok_result(data={"bookmarked": True})

        async def read_bookmark_state(self, post_url: str) -> Any:
            return ok_result(data={"bookmark_state": "bookmarked"})

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]

    fake = _FakeM5BookmarkBroker()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=d._kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(fake, gateway, policies=DEFAULT_EFFECT_POLICIES)
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    executor = M5EffectExecutor(runtime=runtime, evidence_reader=fake)  # type: ignore[arg-type]
    d._m5_stack = SimpleNamespace(effect_executor=executor)  # type: ignore[assignment]
    d._m5_canary_adapters.clear()
    d._write_kernel._write_broker_factory = lambda: object()  # type: ignore[attr-defined]

    flow = CardFlow(d.invoke)
    r1, card = await flow.begin(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    assert card is not None
    assert "bookmark" in card.summary.lower()

    r2 = await card.approve(d.invoke)
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    assert fake.bookmark_clicks == ["https://x.com/jack/status/20"]


# ---------------------------------------------------------------------------
# Rule lifecycle: list + TTL re-confirm
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> RuleStore:
    return RuleStore(tmp_path / "rules.json", clock=lambda: NOW)


def _stored_rule(rule_id: str, decision: RuleDecision = RuleDecision.ALLOW,
                 *, ttl: float = 3600.0, created: float = NOW) -> UserRule:
    return UserRule(
        rule_id=rule_id,
        decision=decision,
        created_at=created,
        expires_at=created + ttl,
        selector=RuleSelector(action_types=frozenset({"like"})),
        source_text="hand written",
    )


def test_list_rules_renders_canonical_descriptions_and_ttl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    aging = _stored_rule("allow-like", RuleDecision.ALLOW, ttl=1000.0)
    never = _stored_rule("never-post", RuleDecision.NEVER, ttl=DEFAULT_RULE_TTL_S)
    store.save([aging, never])
    cards = list_rules(store, now=NOW + 500.0)
    assert [c.rule_id for c in cards] == ["allow-like", "never-post"]
    assert all(isinstance(c, RuleCard) for c in cards)
    live = cards[0]
    assert live.remaining_seconds == 500.0
    assert live.expired is False
    assert live.decision == "allow"
    assert live.provenance == "hand_written"
    assert live.description == describe_rule(aging)
    assert live.description == (
        'ALLOW when actions ["like"] — expires in 1000.0s (16.7 minutes)'
    )
    assert cards[1].remaining_seconds == pytest.approx(
        DEFAULT_RULE_TTL_S - 500.0
    )


def test_list_rules_on_corrupt_store_fails_safe_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_bytes(b"\xff\xfe broken")
    assert list_rules(_store(tmp_path)) == []


def test_reconfirm_preserves_identity_and_other_rules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([
        _stored_rule("keep-me", RuleDecision.NEVER),
        _stored_rule("aging", RuleDecision.ALLOW, ttl=60.0),
    ])
    later = NOW + 50.0  # 10 seconds before 'aging' expires
    reconfirmed = reconfirm_rule(store, "aging", ttl_seconds=3600.0, now=later)
    assert reconfirmed.rule_id == "aging"  # identity — attribution stays valid
    assert reconfirmed.created_at == later
    assert reconfirmed.expires_at == later + 3600.0
    assert reconfirmed.selector == _stored_rule("aging").selector

    loaded = store.load()
    assert [r.rule_id for r in loaded] == ["keep-me", "aging"], "position preserved"
    keep = loaded[0]
    assert keep.created_at == NOW and keep.expires_at == NOW + 3600.0
    assert loaded[1].expires_at == later + 3600.0


def test_reconfirm_revives_expired_rule_by_owner_choice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("old", ttl=10.0)])
    later = NOW + 999.0
    revived = reconfirm_rule(store, "old", ttl_seconds=60.0, now=later)
    assert revived.is_expired(later) is False
    assert list_rules(store, now=later)[0].expired is False


def test_reconfirm_unknown_rule_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("real")])
    with pytest.raises(RuleStoreError, match="nothing to re-confirm"):
        reconfirm_rule(store, "ghost", ttl_seconds=60.0, now=NOW)
    assert [r.rule_id for r in store.load()] == ["real"]


def test_reconfirm_validates_ttl_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("r")])
    for bad in (0, -5, DEFAULT_RULE_TTL_S + 1, True, "x"):
        with pytest.raises(ValueError):
            reconfirm_rule(store, "r", ttl_seconds=bad, now=NOW)  # type: ignore[arg-type]


def test_reconfirm_refuses_corrupt_store_bytes_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("never-keep", RuleDecision.NEVER)])
    p = tmp_path / "rules.json"
    doc = p.read_text(encoding="utf-8")
    import json as _json
    payload = _json.loads(doc)
    payload["rules"].append({"rule_id": "broken"})
    p.write_text(_json.dumps(payload), encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    # The source read fails closed as unknown-rule; the fenced update would
    # refuse too. Either way: no byte changes.
    with pytest.raises(RuleStoreError):
        reconfirm_rule(store, "never-keep", ttl_seconds=60.0, now=NOW)
    assert p.read_text(encoding="utf-8") == before
