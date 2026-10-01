"""M8 layer 4 acceptance tests — the card surface + rule lifecycle + Card
CLI (frozen spec section 8).

The properties under test:

- Token custody is an API property: the phase-1 result RETURNED to the
  caller carries no confirmation token (payload or policy echo); the token
  exists only inside the card, which is bound to the invoke route that
  created it (approve() takes no arguments).
- approve() replays the EXACT deep-copied phase-1 payload; deny() invokes
  nothing; a card is single-use.
- Rule-ALLOW and NEVER outcomes return with NO card.
- The rule lifecycle: list (fail-safe, total over unknown actions, owner's
  words, immutable snapshot) and TTL re-confirm as COMPARE-AND-SWAP (a
  same-id edit between review and confirm conflicts with zero mutation).
- The Card CLI drives all three commands with injected dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.envelope import ok_result
from webwire.m8_card_cli import CardCli
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
# Card flow over a fake invoke: custody, binding, immutability, single-use
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
    return ok_result(
        data={
            "policy": {
                "verdict": verdict,
                "blocked_by": None,
                # The kernel's policy echo carries the token dict too — the
                # sanitizer must scrub BOTH locations.
                "confirmation_token": {"token": token, "intent_hash": "h" * 32},
            },
            "data": data,
            "trace": {"stages": ["confirmation_required"]},
        }
    )


def _allow_result() -> Any:
    return ok_result(
        data={
            "policy": {"verdict": "allow", "blocked_by": None},
            "data": {"liked": "1"},
            "trace": {"stages": ["confirmed", "execute_attempted"], "approver": "human"},
        }
    )


async def test_F40_returned_result_carries_no_token_anywhere() -> None:
    invoke = _FakeInvoke([_phase1_result()])
    result, card = await CardFlow(invoke).begin("like_post", {"post_id": "1"})
    assert card is not None
    # Payload location scrubbed.
    assert "confirmation_token" not in result.data["data"]
    # Policy echo scrubbed (the raw kernel result carries it in both).
    assert not result.data["policy"].get("confirmation_token")
    # The card still holds it internally — hidden from repr/dict/render.
    assert "tok-1" not in repr(card) + card.render_text() + str(card.to_dict())


async def test_F38_card_is_bound_to_its_invoke_approve_is_parameterless() -> None:
    invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    _, card = await CardFlow(invoke).begin("like_post", {"post_id": "1"})
    result = await card.approve()  # no caller-selected route
    assert result.data["policy"]["verdict"] == "allow"
    name, payload = invoke.calls[1]
    assert name == "like_post"
    assert payload == {"post_id": "1", "confirmation_token": "tok-1"}


async def test_F39_replay_is_the_phase1_payload_not_caller_state() -> None:
    invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    payload = {"post_id": "1", "post": {"text": "A"}}
    _, card = await CardFlow(invoke).begin("like_post", payload)
    payload["post"]["text"] = "B"  # caller mutates nested state after phase 1
    await card.approve()
    replayed = invoke.calls[1][1]
    assert replayed["post"] == {"text": "A"}, "replay must be the phase-1 snapshot"


async def test_F43_payload_frozen_before_the_first_await() -> None:
    """The await window: the invoke itself mutates the caller's nested
    payload WHILE phase 1 is in flight. The request snapshot is taken
    before the await, so the card still replays the phase-1 values."""
    victim = {"post_id": "1", "post": {"text": "A"}}
    results = [_phase1_result(), _allow_result()]
    calls: list[tuple[str, dict]] = []

    class _MutatingInvoke:
        async def __call__(self, capability_name: str, payload: dict):
            # Suspended inside phase 1: the caller's dict changes NOW.
            victim["post"]["text"] = "MUTATED-DURING-AWAIT"
            calls.append((capability_name, dict(payload)))
            return results.pop(0)

    _, card = await CardFlow(_MutatingInvoke()).begin("like_post", victim)
    await card.approve()
    replayed = calls[1][1]
    assert replayed["post"] == {"text": "A"}, "the card must replay the snapshot frozen BEFORE the await"


async def test_F47_scrub_is_unconditional_regardless_of_envelope_shape() -> None:
    """A drifted envelope — confirmation token present ONLY in the policy
    echo, or a non-dict policy token — must never leak, and a payload-less
    token cannot produce a card (fail-closed)."""
    # Token only in the policy echo: no card, and the echo is scrubbed.
    drifted = ok_result(
        data={
            "policy": {
                "verdict": "confirmation_required",
                "confirmation_token": {"token": "tok-x"},
            },
            "data": {"preview": "Will like 1"},
            "trace": {},
        }
    )
    result, card = await CardFlow(_FakeInvoke([drifted])).begin("like_post", {"post_id": "1"})
    assert card is None, "a payload-less token cannot be approved via a card"
    assert "confirmation_token" not in result.data["policy"]
    assert "tok-x" not in str(result.data)

    # Non-dict policy token value: removed regardless of type; the valid
    # payload token still yields a card with full custody.
    weird = ok_result(
        data={
            "policy": {
                "verdict": "confirmation_required",
                "confirmation_token": "tok-y",
            },
            "data": {
                "preview": "Will like 1",
                "confirmation_token": "tok-y",
                "expires_at": 1.0,
            },
            "trace": {},
        }
    )
    result2, card2 = await CardFlow(_FakeInvoke([weird])).begin("like_post", {"post_id": "1"})
    assert "confirmation_token" not in result2.data["policy"]
    assert "confirmation_token" not in result2.data["data"]
    assert "tok-y" not in str(result2.data)
    assert card2 is not None
    assert "tok-y" not in repr(card2) + card2.render_text()


async def test_deny_invokes_nothing_and_consumes_the_card() -> None:
    invoke = _FakeInvoke([_phase1_result()])
    _, card = await CardFlow(invoke).begin("like_post", {"post_id": "1"})
    denied = card.deny()
    assert denied.ok is False
    assert "denied by owner" in denied.error.message
    assert len(invoke.calls) == 1, "deny must not invoke anything"
    with pytest.raises(RuntimeError, match="already denied"):
        card.deny()
    with pytest.raises(RuntimeError, match="already denied"):
        await card.approve()


async def test_card_is_single_use_on_approve() -> None:
    invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    _, card = await CardFlow(invoke).begin("like_post", {"post_id": "1"})
    await card.approve()
    with pytest.raises(RuntimeError, match="already approved"):
        await card.approve()
    assert len(invoke.calls) == 2


async def test_rule_allow_and_never_return_no_card() -> None:
    allow = ok_result(
        data={
            "policy": {"verdict": "allow", "blocked_by": None},
            "data": {"liked": "1"},
            "trace": {"stages": ["rule_approved"], "approver": "rule:allow-like"},
        }
    )
    result, card = await CardFlow(_FakeInvoke([allow])).begin("like_post", {"post_id": "1"})
    assert card is None
    assert result.data["policy"]["verdict"] == "allow"

    deny = ok_result(
        data={
            "policy": {"verdict": "deny", "blocked_by": "user_rule"},
            "data": {"rule_id": "ban-like", "decision": "never"},
            "trace": {"stages": ["denied:user_rule"]},
        }
    )
    deny.ok = False
    result, card = await CardFlow(_FakeInvoke([deny])).begin("like_post", {"post_id": "1"})
    assert card is None
    assert result.data["policy"]["blocked_by"] == "user_rule"


async def test_ask_card_surfaces_matched_rule_and_warnings() -> None:
    matched = {"matched_rule_id": "ask-like", "ceiling_downgraded": False}
    _, card = await CardFlow(
        _FakeInvoke(
            [
                _phase1_result(matched_rule=matched, warnings=["post is from a stranger"]),
            ]
        )
    ).begin("like_post", {"post_id": "1"})
    assert card is not None
    assert card.matched_rule == {"matched_rule_id": "ask-like", "ceiling_downgraded": False}
    text = card.render_text()
    assert "ask-like" in text
    assert "post is from a stranger" in text
    assert "Will like post 1" in text


# ---------------------------------------------------------------------------
# Card flow over a REAL dispatcher (phase 1 → card → phase 2 executed)
# ---------------------------------------------------------------------------


async def _live_bookmark_dispatcher(tmp_path: Path):
    """A real Dispatcher over a stubbed session with the M5 bookmark stack
    installed (the test_bookmark_dispatch._install_fake_m5 pattern). Returns
    (dispatcher, fake_broker, config)."""
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
    return d, fake, cfg


async def test_card_drives_real_dispatcher_phase1_phase2(tmp_path: Path) -> None:
    """End to end through the ordinary authority path: the card's approve()
    consumes a REAL kernel token through the bound route and the bookmark
    executes."""
    d, fake, _cfg = await _live_bookmark_dispatcher(tmp_path)

    flow = CardFlow(d.invoke)
    r1, card = await flow.begin("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    assert card is not None
    assert "bookmark" in card.summary.lower()
    assert "confirmation_token" not in r1.data["data"], "custody is an API property"

    r2 = await card.approve()
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    assert fake.bookmark_clicks == ["https://x.com/jack/status/20"]


async def test_F48_caller_supplied_token_rejected_before_anything_runs() -> None:
    """The card entry point never accepts pre-existing confirmation
    authority: a payload carrying confirmation_token would make the kernel
    treat begin() as PHASE 2 and execute before any card or decision.
    Rejected — not stripped — with ZERO invocations."""
    invoke = _FakeInvoke([])
    with pytest.raises(ValueError, match="caller-supplied"):
        await CardFlow(invoke).begin("like_post", {"post_id": "1", "confirmation_token": "T"})
    assert invoke.calls == []


async def test_F50_live_token_never_reaches_the_journal(tmp_path: Path) -> None:
    """The stronger F-50 regression: a REAL kernel token, a NEVER installed
    after phase 1 denies BEFORE the confirmation gate consumes it, and the
    exact token bytes never appear in the audit journal — the record is
    redacted by key."""
    from webwire.safety.user_rules import RuleSelector, UserRule

    d, _fake, cfg = await _live_bookmark_dispatcher(tmp_path)
    payload = {"post_url": "https://x.com/jack/status/20"}

    # Phase 1 through the raw dispatcher (the caller's own authority here):
    # a REAL confirmation token is minted.
    r1 = await d.invoke("bookmark_post", dict(payload))
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    token = r1.data["data"]["confirmation_token"]

    # A NEVER arrives after phase 1 — the rule gate runs BEFORE token
    # validation and consumption (T14 ordering).
    from webwire.safety.user_rules import RuleDecision, RuleStore

    RuleStore(cfg.rules_path()).save(
        [
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"bookmark"})),
                decision=RuleDecision.NEVER,
                rule_id="late-ban",
            ),
        ]
    )
    r2 = await d.invoke("bookmark_post", {**payload, "confirmation_token": token})
    assert r2.data["policy"]["blocked_by"] == "user_rule"

    # The denial happened BEFORE consumption: remove the ban and the SAME
    # token still carries phase 2 to execution.
    cfg.rules_path().unlink()
    r3 = await d.invoke("bookmark_post", {**payload, "confirmation_token": token})
    assert r3.data["policy"]["verdict"] == "allow"
    assert r3.data["trace"]["execute_ok"] is True

    # The journal is audit data, never an authority carrier: the exact
    # token bytes are absent, and the denial record redacts by key (the
    # journal serializes compact — no space after the colon).
    journal = cfg.journal_path().read_text(encoding="utf-8")
    assert token not in journal, "a live token must never be journaled verbatim"
    assert '"confirmation_token":"<redacted>"' in journal


# ---------------------------------------------------------------------------
# Rule lifecycle: list + compare-and-swap TTL re-confirm
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> RuleStore:
    return RuleStore(tmp_path / "rules.json", clock=lambda: NOW)


def _stored_rule(
    rule_id: str,
    decision: RuleDecision = RuleDecision.ALLOW,
    *,
    ttl: float = 3600.0,
    created: float = NOW,
    action: str = "like",
    source_text: str = "hand written",
) -> UserRule:
    return UserRule(
        rule_id=rule_id,
        decision=decision,
        created_at=created,
        expires_at=created + ttl,
        selector=RuleSelector(action_types=frozenset({action})),
        source_text=source_text,
    )


def test_list_rules_renders_words_description_and_ttl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    aging = _stored_rule("allow-like", RuleDecision.ALLOW, ttl=1000.0, source_text="likes from me are fine")
    never = _stored_rule("never-post", RuleDecision.NEVER, ttl=DEFAULT_RULE_TTL_S, action="post")
    store.save([aging, never])
    cards = list_rules(store, now=NOW + 500.0)
    assert [c.rule_id for c in cards] == ["allow-like", "never-post"]
    assert all(isinstance(c, RuleCard) for c in cards)
    live = cards[0]
    assert live.remaining_seconds == 500.0
    assert live.expired is False
    assert live.provenance == "hand_written"
    assert live.source_text == "likes from me are fine"  # F-36
    assert live.rule == aging  # the immutable snapshot re-confirm binds to
    assert live.description == ('ALLOW when actions ["like"] — expires in 1000.0s (16.7 minutes)')
    assert "likes from me are fine" in live.render_text()
    assert "owner's words" in live.render_text()


def test_F35_listing_is_total_over_unknown_actions(tmp_path: Path) -> None:
    """A hand-written rule naming an action outside the registry is LEGAL
    store content; listing it must not raise, must not infer a tier, and
    must say the ceiling is unverifiable for ALLOW."""
    store = _store(tmp_path)
    store.save(
        [
            _stored_rule("custom-allow", RuleDecision.ALLOW, action="custom_action"),
            _stored_rule("custom-never", RuleDecision.NEVER, action="another_custom"),
        ]
    )
    cards = list_rules(store, now=NOW)
    assert [c.rule_id for c in cards] == ["custom-allow", "custom-never"]
    allow_desc = cards[0].description
    assert "not verifiable" in allow_desc
    assert "outside the active registry" in allow_desc
    assert "custom_action" in allow_desc
    # A non-ALLOW decision never carries a ceiling note — and still renders.
    assert "ASK" not in cards[1].description


def test_F49_mixed_action_tier_selector_gets_the_latent_warning() -> None:
    """F-49: the action vocabulary is checked even when risk_tiers is also
    specified. A stored ALLOW naming an unknown action WITH an explicit tier
    is just as latent — currently non-executable, possibly live authority
    after a future registration — and the note says the tier must MATCH the
    selector too."""
    from webwire.safety.models import RiskTier

    mixed = UserRule(
        rule_id="mixed",
        decision=RuleDecision.ALLOW,
        created_at=NOW,
        expires_at=NOW + 3600.0,
        selector=RuleSelector(
            action_types=frozenset({"future_action"}),
            risk_tiers=frozenset({RiskTier.PRIVATE_REVERSIBLE}),
        ),
        source_text="",
    )
    desc = describe_rule(mixed)
    assert "outside the active registry" in desc
    assert "currently non-executable" in desc
    assert "may auto-approve only if" in desc
    assert "below-ceiling tiers matching this selector" in desc
    assert "ASK" not in desc.split("—")[0], "no unconditional ASK promise"


def test_list_rules_on_corrupt_store_fails_safe_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_bytes(b"\xff\xfe broken")
    assert list_rules(_store(tmp_path)) == []


def test_reconfirm_preserves_identity_and_other_rules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(
        [
            _stored_rule("keep-me", RuleDecision.NEVER),
            _stored_rule("aging", ttl=60.0, source_text="temporary allow"),
        ]
    )
    expected = store.load()[1]  # the reviewed snapshot
    later = NOW + 50.0
    reconfirmed = reconfirm_rule(store, expected, ttl_seconds=3600.0, now=later)
    assert reconfirmed.rule_id == "aging"  # identity — attribution stays valid
    assert reconfirmed.source_text == "temporary allow"  # the owner's words kept
    assert reconfirmed.created_at == later
    assert reconfirmed.expires_at == later + 3600.0

    loaded = store.load()
    assert [r.rule_id for r in loaded] == ["keep-me", "aging"], "position preserved"
    assert loaded[0].expires_at == NOW + 3600.0  # untouched
    assert loaded[1].expires_at == later + 3600.0


def test_F34_stale_reconfirm_cannot_overwrite_a_newer_never(tmp_path: Path) -> None:
    """The exact race from the review: the owner reviewed ALLOW; the stored
    rule became NEVER before the re-confirm landed. The stale re-confirm
    must conflict with ZERO mutation — the NEVER survives."""
    store = _store(tmp_path)
    store.save([_stored_rule("r1", RuleDecision.ALLOW)])
    reviewed = store.load()[0]  # the owner saw ALLOW

    # Another writer flips the same id to NEVER (deliberate whole-document
    # replacement — the only sanctioned unconditional write).
    store.save([_stored_rule("r1", RuleDecision.NEVER)])

    with pytest.raises(RuleStoreError, match="changed since review"):
        reconfirm_rule(store, reviewed, ttl_seconds=3600.0, now=NOW)
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].decision is RuleDecision.NEVER, "the NEVER must survive"
    assert loaded[0].expires_at == NOW + 3600.0  # zero mutation of it


def test_F34_reconfirm_of_deleted_rule_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("r1")])
    reviewed = store.load()[0]
    store.save([])  # deletion via whole-document replace
    with pytest.raises(RuleStoreError, match="no longer present"):
        reconfirm_rule(store, reviewed, ttl_seconds=60.0, now=NOW)
    assert store.load() == []


def test_replace_if_current_refuses_identity_rewrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("r1")])
    with pytest.raises(RuleStoreError, match="cannot rewrite identity"):
        store.replace_if_current(_stored_rule("r1"), _stored_rule("other-id"))


def test_reconfirm_unknown_rule_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("real")])
    with pytest.raises(RuleStoreError, match="no longer present"):
        reconfirm_rule(store, _stored_rule("ghost"), ttl_seconds=60.0, now=NOW)
    assert [r.rule_id for r in store.load()] == ["real"]


def test_reconfirm_validates_ttl_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("r")])
    expected = store.load()[0]
    for bad in (0, -5, DEFAULT_RULE_TTL_S + 1, True, "x"):
        with pytest.raises(ValueError):
            reconfirm_rule(store, expected, ttl_seconds=bad, now=NOW)  # type: ignore[arg-type]


def test_reconfirm_refuses_corrupt_store_bytes_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("never-keep", RuleDecision.NEVER)])
    reviewed = store.load()[0]
    p = tmp_path / "rules.json"
    import json as _json

    payload = _json.loads(p.read_text(encoding="utf-8"))
    payload["rules"].append({"rule_id": "broken"})
    p.write_text(_json.dumps(payload), encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    with pytest.raises(RuleStoreError):
        reconfirm_rule(store, reviewed, ttl_seconds=60.0, now=NOW)
    assert p.read_text(encoding="utf-8") == before


def test_reconfirm_revives_expired_rule_by_owner_choice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("old", ttl=10.0)])
    later = NOW + 999.0
    reviewed = store.load()[0]
    revived = reconfirm_rule(store, reviewed, ttl_seconds=60.0, now=later)
    assert revived.is_expired(later) is False
    assert list_rules(store, now=later)[0].expired is False


# ---------------------------------------------------------------------------
# The Card CLI (frozen build-order step 4) — chronological decisions (F-42),
# production wiring (F-41), controlled TTL errors (F-46), browser-free rules
# commands (F-44)
# ---------------------------------------------------------------------------


def _cli(tmp_path: Path, runtime_factory=None, decision_reader=None) -> CardCli:
    return CardCli(
        store=_store(tmp_path),
        runtime_factory=runtime_factory,
        decision_reader=decision_reader or (lambda prompt: "n"),
        clock=lambda: NOW,
    )


def _ready(runtime):
    async def _factory():
        return runtime

    return _factory


class _FakeRuntime:
    """A started fake dispatcher for cmd_card: scripted results + ordered
    lifecycle recording (F-41 wiring at the command level)."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls: list[str] = []
        self.stopped = False

    async def invoke(self, capability: str, payload: dict):
        self.calls.append(f"invoke:{capability}")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def stop(self) -> None:
        self.stopped = True


async def test_cli_rules_list_renders_words_and_ttl(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path).run(["rules", "list"])
    assert code == 0  # empty store is not an error
    assert "no rules stored" in capsys.readouterr().out
    store = _store(tmp_path)
    store.save([_stored_rule("allow-like", source_text="likes are fine")])
    code = await _cli(tmp_path).run(["rules", "list"])
    out = capsys.readouterr().out
    assert code == 0
    assert "allow-like" in out
    assert "likes are fine" in out
    assert 'actions ["like"]' in out


async def test_cli_rules_commands_never_build_a_runtime(tmp_path: Path) -> None:
    """Rules commands are browser-free: the runtime factory would fail the
    test if ever invoked."""

    def _forbidden_factory():
        raise AssertionError("rules commands must never build a runtime")

    cli = CardCli(
        store=_store(tmp_path),
        runtime_factory=_forbidden_factory,
        decision_reader=lambda prompt: "n",
        clock=lambda: NOW,
    )
    assert await cli.run(["rules", "list"]) == 0


async def test_cli_reconfirm_decision_is_chronological(tmp_path, capsys) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("aging", ttl=60.0, source_text="temporary")])

    seen_at_decision: dict[str, bool] = {}

    def reader_n(prompt: str) -> str:
        out = capsys.readouterr().out
        seen_at_decision["words_displayed"] = "temporary" in out
        seen_at_decision["structure_displayed"] = 'actions ["like"]' in out
        return "n"

    code = await _cli(tmp_path, decision_reader=reader_n).run(["rules", "reconfirm", "aging"])
    assert code == 0
    assert "declined" in capsys.readouterr().out
    assert seen_at_decision["words_displayed"], "decision must follow display"
    assert seen_at_decision["structure_displayed"]
    assert store.load()[0].expires_at == NOW + 60.0  # zero mutation

    def reader_y(prompt: str) -> str:
        return "y"

    code = await _cli(tmp_path, decision_reader=reader_y).run(["rules", "reconfirm", "aging", "--ttl", "120"])
    assert code == 0
    assert "re-confirmed" in capsys.readouterr().out
    assert store.load()[0].expires_at == NOW + 120.0


async def test_cli_reconfirm_ttl_errors_are_controlled(tmp_path, capsys) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("r")])
    for bad in ("0", "-1", "nan", "inf", "999999999"):
        code = await _cli(tmp_path, decision_reader=lambda p: "y").run(
            ["rules", "reconfirm", "r", "--ttl", bad]
        )
        assert code == 2, bad
        assert "ttl_seconds" in capsys.readouterr().err
    assert store.load()[0].expires_at == NOW + 3600.0  # never mutated


async def test_cli_reconfirm_unknown_rule_is_an_error(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path).run(["rules", "reconfirm", "ghost"])
    assert code == 1
    assert "not found" in capsys.readouterr().err


async def test_cli_card_decision_follows_display_and_binds(tmp_path, capsys) -> None:
    runtime = _FakeRuntime([_phase1_result(), _allow_result()])

    def reader_y(prompt: str) -> str:
        out = capsys.readouterr().out
        assert "Will like post 1" in out, "the decision must follow display"
        assert "confirmation_token" not in out
        return "y"

    code = await _cli(tmp_path, runtime_factory=_ready(runtime), decision_reader=reader_y).run(
        ["card", "like_post", '{"post_id": "1"}']
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "approved: verdict='allow'" in out
    assert runtime.calls == ["invoke:like_post", "invoke:like_post"]
    assert runtime.stopped, "the runtime must stop after the decision"


async def test_cli_card_decline_executes_nothing_and_stops_runtime(tmp_path, capsys) -> None:
    runtime = _FakeRuntime([_phase1_result()])
    code = await _cli(tmp_path, runtime_factory=_ready(runtime)).run(
        ["card", "like_post", '{"post_id": "1"}']
    )  # default reader: "n"
    out = capsys.readouterr().out
    assert code == 0
    assert "denied" in out
    assert runtime.calls == ["invoke:like_post"]  # phase 2 never ran
    assert runtime.stopped


async def test_cli_card_stops_runtime_even_when_approval_raises(tmp_path, capsys) -> None:
    class _BoomRuntime(_FakeRuntime):
        async def invoke(self, capability, payload):
            if len(self.calls) >= 1:
                raise RuntimeError("phase 2 exploded")
            return await super().invoke(capability, payload)

    runtime = _BoomRuntime([_phase1_result(), _allow_result()])
    with pytest.raises(RuntimeError, match="phase 2 exploded"):
        await _cli(
            tmp_path,
            runtime_factory=_ready(runtime),
            decision_reader=lambda p: "y",
        ).run(["card", "like_post", '{"post_id": "1"}'])
    assert runtime.stopped, "stop() must run in the finally"


async def test_cli_card_bad_payload_is_usage_error(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path, runtime_factory=_ready(_FakeRuntime([]))).run(["card", "like_post", "not json"])
    assert code == 2
    assert capsys.readouterr().err


async def test_cli_card_without_runtime_errors(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path).run(["card", "like_post", '{"post_id": "1"}'])
    assert code == 1
    assert "no live runtime" in capsys.readouterr().err


async def test_F52_malformed_confirmation_required_is_an_error(tmp_path, capsys) -> None:
    """F-52: a confirmation-required response with no usable card is a
    PROTOCOL failure — neither execution nor approval authority exists, so
    the CLI must exit non-zero, never success."""
    drifted = ok_result(
        data={
            "policy": {
                "verdict": "confirmation_required",
                "confirmation_token": {"token": "tok-x"},
            },
            "data": {"preview": "Will like 1"},
            "trace": {},
        }
    )
    runtime = _FakeRuntime([drifted])
    code = await _cli(tmp_path, runtime_factory=_ready(runtime)).run(["card", "like_post", '{"post_id": "1"}'])
    err = capsys.readouterr().err
    assert code == 1
    assert "no usable approval carrier" in err
    assert runtime.calls == ["invoke:like_post"]  # phase 2 never ran
    assert runtime.stopped


# ---------------------------------------------------------------------------
# F-41: the PRODUCTION runtime factory wiring
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Injectable dispatcher double: records lifecycle order, can fail."""

    def __init__(self, *, whoami_ok=True, whoami_handle="@owner", start_ok=True, session=None) -> None:
        self.order: list[str] = []
        self._whoami_ok = whoami_ok
        self._whoami_handle = whoami_handle
        self._start_ok = start_ok
        self.session = session
        self.stopped = False

    async def start(self):
        self.order.append("start")
        from webwire.envelope import ok_result

        if not self._start_ok:
            r = ok_result(data={})
            r.ok = False
            r.error = type("E", (), {"message": "start failed"})()
            return r
        return ok_result(data={})

    async def invoke(self, capability, payload):
        self.order.append(f"invoke:{capability}")
        from webwire.envelope import ok_result

        if capability == "whoami":
            if not self._whoami_ok:
                r = ok_result(data={})
                r.ok = False
                r.error = type("E", (), {"message": "not logged in"})()
                return r
            # Mirror the real post-whoami hook: set_resolved_handle strips
            # and refuses blank values, so a whitespace handle leaves the
            # session's actor state unset (F-51).
            if self._whoami_handle.strip():
                self.session.resolved_handle = self._whoami_handle.strip()
            return ok_result(data={"handle": self._whoami_handle})
        raise AssertionError(f"unexpected invoke {capability}")

    async def stop(self):
        self.order.append("stop")
        self.stopped = True


class _FakeSession:
    def __init__(self, config) -> None:
        self.resolved_handle = None


async def test_F41_production_runtime_wiring(tmp_path: Path) -> None:
    from webwire.config import WebWireConfig
    from webwire.m8_card_cli import build_production_runtime

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    made: list = []

    def dispatcher_factory(config, *, session_manager):
        d = _RecordingDispatcher(session=session_manager)
        made.append(d)
        return d

    # Success: start → whoami → resolved actor; NOT stopped by the factory.
    await build_production_runtime(cfg, dispatcher_factory=dispatcher_factory, session_factory=_FakeSession)
    assert made[0].order == ["start", "invoke:whoami"]
    assert made[0].session.resolved_handle == "@owner"
    assert made[0].stopped is False, "the caller owns stop()"

    def bad_factory(config, *, session_manager):
        return _RecordingDispatcher(whoami_ok=False, session=session_manager)

    with pytest.raises(RuntimeError, match="no verified actor"):
        await build_production_runtime(cfg, dispatcher_factory=bad_factory, session_factory=_FakeSession)

    def handleless_factory(config, *, session_manager):
        return _RecordingDispatcher(whoami_handle="", session=session_manager)

    with pytest.raises(RuntimeError, match="resolved actor"):
        await build_production_runtime(cfg, dispatcher_factory=handleless_factory, session_factory=_FakeSession)

    # F-51: a whitespace-only response handle is TRUTHY response data but
    # set_resolved_handle refuses it — the factory must verify the actor
    # state the write path trusts, and that state is unset here.
    def blank_handle_factory(config, *, session_manager):
        return _RecordingDispatcher(whoami_handle="   ", session=session_manager)

    with pytest.raises(RuntimeError, match="resolved actor"):
        await build_production_runtime(
            cfg, dispatcher_factory=blank_handle_factory, session_factory=_FakeSession
        )

    start_failed = _RecordingDispatcher(start_ok=False)

    def start_fail_factory(config, *, session_manager):
        return start_failed

    with pytest.raises(RuntimeError, match="start failed"):
        await build_production_runtime(cfg, dispatcher_factory=start_fail_factory, session_factory=_FakeSession)
    assert start_failed.stopped


# ---------------------------------------------------------------------------
# F-44: the rules-only CLI is importable with NO browser dependency at all
# ---------------------------------------------------------------------------


def test_F44_rules_cli_imports_without_the_browser_dependency(tmp_path: Path) -> None:
    """A clean subprocess (the conftest stub path REMOVED, as on a base
    installation) imports the CLI and runs a rules command. Importing must
    not pull super_browser, the envelope, or the dispatcher."""
    import os
    import subprocess
    import sys

    rules_path = tmp_path / "rules.json"
    code = (
        "import sys\n"
        "sys.path = [p for p in sys.path if 'stubs' not in p]\n"
        "from webwire.m8_card_cli import CardCli\n"
        "from webwire.safety.user_rules import RuleStore\n"
        "leaked = [m for m in sys.modules\n"
        "          if 'super_browser' in m or m in ('webwire.envelope',\n"
        "          'webwire.dispatcher', 'webwire.safety.write_kernel')]\n"
        "assert not leaked, f'browser chain leaked: {leaked}'\n"
        "import asyncio\n"
        "cli = CardCli(store=RuleStore(__import__('pathlib').Path(sys.argv[1])),\n"
        "              decision_reader=lambda p: 'n')\n"
        "rc = asyncio.run(cli.run(['rules', 'list']))\n"
        "assert rc == 0\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    parent_path = [os.path.abspath(e) for e in sys.path if e and "stubs" not in e]
    inherited = env.get("PYTHONPATH", "")
    combined = parent_path + ([inherited] if inherited else [])
    env["PYTHONPATH"] = os.pathsep.join(p for p in combined if "stubs" not in p)
    result = subprocess.run(
        [sys.executable, "-c", code, str(rules_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout  # rules output may precede the marker


# ---------------------------------------------------------------------------
# F-55 / F-54 / F-56 (final round): composed ceiling semantics, CLI-early
# reserved-field rejection, genuine-string carrier requirement
# ---------------------------------------------------------------------------


def _unknown_action_rule(tiers: frozenset | None) -> UserRule:

    return UserRule(
        rule_id="latent",
        decision=RuleDecision.ALLOW,
        created_at=NOW,
        expires_at=NOW + 3600.0,
        selector=RuleSelector(
            action_types=frozenset({"future_action"}),
            risk_tiers=frozenset(tiers) if tiers is not None else None,
        ),
        source_text="",
    )


def test_F55_unknown_action_composes_with_explicit_tier_ceiling() -> None:
    """With explicit tiers, whether auto-approval is even POSSIBLE is
    determined by those tiers — the unknown-action warning must compose
    with the ceiling math, not mask it (frozen §5: above-ceiling ALLOW is
    honestly ASK)."""
    from webwire.safety.models import RiskTier

    # All named tiers above the ceiling: any future matching registration
    # still ASKs — auto-approval is impossible, and the text says so.
    all_above = describe_rule(_unknown_action_rule(frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE})))
    assert "currently non-executable" in all_above
    assert "will still ASK" in all_above
    assert "every named tier is above the allow ceiling" in all_above
    assert "may auto-approve" not in all_above

    # Mixed tiers: auto-approval possible ONLY at the below-ceiling tier;
    # the above-ceiling tier still ASKs.
    mixed = describe_rule(
        _unknown_action_rule(frozenset({RiskTier.PRIVATE_REVERSIBLE, RiskTier.PUBLIC_CONTENT_IRREVERSIBLE}))
    )
    assert "may auto-approve only if" in mixed
    assert "below-ceiling tiers matching this selector" in mixed
    assert "above-ceiling tiers still ASK" in mixed

    # All below the ceiling: the only auto-approval route; no ASK claim.
    all_below = describe_rule(_unknown_action_rule(frozenset({RiskTier.PRIVATE_REVERSIBLE})))
    assert "may auto-approve only if" in all_below
    assert "still ASK" not in all_below

    # No explicit tiers: the F-49 latent-authority wording stands.
    no_tiers = describe_rule(_unknown_action_rule(None))
    assert "may auto-approve" in no_tiers
    assert "currently non-executable" in no_tiers


async def test_F54_cli_rejects_reserved_token_before_building_runtime(tmp_path, capsys) -> None:
    """The forbidden field is a USAGE error before ANY live-session work:
    the runtime factory must never be invoked."""

    def _forbidden_factory():
        raise AssertionError("runtime must not be built for forbidden input")

    cli = CardCli(
        store=_store(tmp_path),
        runtime_factory=_forbidden_factory,
        decision_reader=lambda p: "y",
        clock=lambda: NOW,
    )
    code = await cli.run(["card", "like_post", '{"post_id": "1", "confirmation_token": "T"}'])
    err = capsys.readouterr().err
    assert code == 2
    assert "reserved confirmation_token" in err


async def test_F56_malformed_token_carrier_never_becomes_a_card(
    tmp_path: Path,
) -> None:
    """A non-string confirmation carrier (dict, number) is not usable
    authority: no card is constructed, the sanitized result is returned,
    and the CLI reports the confirmation-required protocol error."""
    for bad_carrier in ({"token": "abc"}, 123, ""):
        payload = {
            "preview": "Will like 1",
            "confirmation_token": bad_carrier,
            "expires_at": NOW + 300.0,
        }
        drifted = ok_result(
            data={
                "policy": {
                    "verdict": "confirmation_required",
                    "confirmation_token": bad_carrier,
                },
                "data": payload,
                "trace": {},
            }
        )
        result, card = await CardFlow(_FakeInvoke([drifted])).begin("like_post", {"post_id": "1"})
        assert card is None, f"a {type(bad_carrier).__name__} carrier is not a card"
        assert "confirmation_token" not in result.data["data"]
        assert "confirmation_token" not in result.data["policy"]

    # The CLI turns the same shape into the controlled protocol error.
    drifted_cli = ok_result(
        data={
            "policy": {
                "verdict": "confirmation_required",
                "confirmation_token": {"token": "abc"},
            },
            "data": {"preview": "Will like 1", "confirmation_token": {"token": "abc"}},
            "trace": {},
        }
    )
    runtime = _FakeRuntime([drifted_cli])
    code = await _cli(tmp_path, runtime_factory=_ready(runtime)).run(["card", "like_post", '{"post_id": "1"}'])
    assert code == 1
    assert runtime.calls == ["invoke:like_post"]
    assert runtime.stopped
