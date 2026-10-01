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


async def test_card_drives_real_dispatcher_phase1_phase2(tmp_path: Path) -> None:
    """End to end through the ordinary authority path: a stubbed session and
    a fake M5 bookmark broker; the card's approve() consumes a REAL kernel
    token through the bound route and the bookmark executes."""
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
    r1, card = await flow.begin("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    assert card is not None
    assert "bookmark" in card.summary.lower()
    assert "confirmation_token" not in r1.data["data"], "custody is an API property"

    r2 = await card.approve()
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    assert fake.bookmark_clicks == ["https://x.com/jack/status/20"]


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

    # Another writer flips the same id to NEVER (fenced update).
    store.update_strict(_stored_rule("r1", RuleDecision.NEVER))

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
# The Card CLI (frozen build-order step 4)
# ---------------------------------------------------------------------------


def _cli(tmp_path: Path, invoke=None) -> CardCli:
    return CardCli(store=_store(tmp_path), invoke=invoke, clock=lambda: NOW)


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


async def test_cli_reconfirm_dry_run_then_confirmed(tmp_path: Path, capsys) -> None:
    store = _store(tmp_path)
    store.save([_stored_rule("aging", ttl=60.0, source_text="temporary")])

    code = await _cli(tmp_path).run(["rules", "reconfirm", "aging"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry run" in out
    assert store.load()[0].expires_at == NOW + 60.0  # nothing changed

    code = await _cli(tmp_path).run(["rules", "reconfirm", "aging", "--ttl", "120", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "re-confirmed" in out
    assert store.load()[0].expires_at == NOW + 120.0
    assert "temporary" in out  # the owner's words shown before confirming


async def test_cli_reconfirm_unknown_rule_is_an_error(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path).run(["rules", "reconfirm", "ghost", "--yes"])
    assert code == 1
    assert "not found" in capsys.readouterr().err


async def test_cli_card_pending_without_flag(tmp_path: Path, capsys) -> None:
    invoke = _FakeInvoke([_phase1_result()])
    code = await _cli(tmp_path, invoke).run(["card", "like_post", '{"post_id": "1"}'])
    out = capsys.readouterr().out
    assert code == 0
    assert "Will like post 1" in out
    assert "pending" in out
    assert len(invoke.calls) == 1  # nothing executed
    assert "confirmation_token" not in invoke.calls[0][1]


async def test_cli_card_approve_and_deny(tmp_path: Path, capsys) -> None:
    approve_invoke = _FakeInvoke([_phase1_result(), _allow_result()])
    code = await _cli(tmp_path, approve_invoke).run(["card", "like_post", '{"post_id": "1"}', "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "approved: verdict='allow'" in out
    assert approve_invoke.calls[1][1]["confirmation_token"] == "tok-1"

    deny_invoke = _FakeInvoke([_phase1_result()])
    code = await _cli(tmp_path, deny_invoke).run(["card", "like_post", '{"post_id": "1"}', "--deny"])
    out = capsys.readouterr().out
    assert code == 0
    assert "denied" in out
    assert len(deny_invoke.calls) == 1


async def test_cli_card_bad_payload_is_usage_error(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path, _FakeInvoke([])).run(["card", "like_post", "not json"])
    assert code == 2
    assert capsys.readouterr().err


async def test_cli_card_without_invoke_route_errors(tmp_path: Path, capsys) -> None:
    code = await _cli(tmp_path, invoke=None).run(["card", "like_post", '{"post_id": "1"}'])
    assert code == 1
    assert "no invoke route" in capsys.readouterr().err
