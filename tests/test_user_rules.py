"""M8 layer 1 tests — the standing-decision store (frozen spec sections 4-5).

Acceptance targets M8-T1 through M8-T9 from docs/M8_DESIGN.md, plus
persistence round-trip and failure semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webwire.safety.models import RiskTier
from webwire.safety.user_rules import (
    ALLOW_CEILING_TIERS,
    DEFAULT_RULE_TTL_S,
    RuleDecision,
    RuleSelector,
    RuleStore,
    UserRule,
)


def _rule(
    decision: RuleDecision,
    *,
    action_types: frozenset[str] | None = None,
    risk_tiers: frozenset[RiskTier] | None = None,
    target_ids: frozenset[str] | None = None,
    actors: frozenset[str] | None = None,
    rule_id: str | None = None,
    ttl: float = DEFAULT_RULE_TTL_S,
    now: float = 1000.0,
    provenance: str = "hand_written",
    source_text: str = "test rule",
) -> UserRule:
    return UserRule(
        rule_id=rule_id or f"r-{decision.value}-{id(object) % 100000}",
        decision=decision,
        created_at=now,
        expires_at=now + ttl,
        provenance=provenance,
        source_text=source_text,
        selector=RuleSelector(
            action_types=action_types, risk_tiers=risk_tiers,
            target_ids=target_ids, actors=actors,
        ),
    )


def _store(tmp_path: Path, rules: list[UserRule]) -> RuleStore:
    store = RuleStore(tmp_path / "rules.json", clock=lambda: 1000.0)
    store.save(rules)
    return RuleStore(tmp_path / "rules.json", clock=lambda: 1000.0)  # fresh read


# ---------------------------------------------------------------------------
# M8-T9: selectors must name their scope
# ---------------------------------------------------------------------------

def test_empty_selector_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="must name its scope"):
        RuleSelector()


def test_invalid_decision_and_provenance_rejected() -> None:
    with pytest.raises(ValueError):
        UserRule(
            rule_id="x", decision="maybe",  # type: ignore[arg-type]
            created_at=0, expires_at=1, selector=RuleSelector(action_types=frozenset({"like"})),
        )
    with pytest.raises(ValueError):
        UserRule(
            rule_id="x", decision=RuleDecision.ALLOW, provenance="guessed",
            created_at=0, expires_at=1, selector=RuleSelector(action_types=frozenset({"like"})),
        )


# ---------------------------------------------------------------------------
# M8-T8: dimension matching
# ---------------------------------------------------------------------------

def test_selector_dimension_mismatch_no_match(tmp_path: Path) -> None:
    store = _store(tmp_path, [_rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}))])
    assert store.match(action_type="post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                       target_id="t", actor="a") is None
    m = store.match(action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.NEVER


def test_unspecified_dimensions_match_anything(tmp_path: Path) -> None:
    store = _store(tmp_path, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}))])
    for tier in RiskTier:
        m = store.match(action_type="bookmark", risk_tier=tier, target_id="anything", actor="anyone")
        assert m is not None, tier


def test_target_scoped_never_rule(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _rule(RuleDecision.NEVER, action_types=frozenset({"reply"}),
              target_ids=frozenset({"999"}), rule_id="never-999"),
        _rule(RuleDecision.ALLOW, action_types=frozenset({"reply"}), rule_id="allow-reply"),
    ])
    a = store.match(action_type="reply", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="999", actor="u")
    assert a is not None and a.decision is RuleDecision.NEVER and a.rule_id == "never-999"
    b = store.match(action_type="reply", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="123", actor="u")
    # reply is above the ceiling → allow downgrades to ask
    assert b is not None and b.decision is RuleDecision.ASK and b.ceiling_downgraded is True


# ---------------------------------------------------------------------------
# M8-T1/T2: the ceiling is a match-time downgrade
# ---------------------------------------------------------------------------

def test_T1_allow_below_ceiling_stands(tmp_path: Path) -> None:
    assert RiskTier.PRIVATE_REVERSIBLE in ALLOW_CEILING_TIERS
    store = _store(tmp_path, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}))])
    m = store.match(action_type="bookmark", risk_tier=RiskTier.PRIVATE_REVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.ALLOW and m.ceiling_downgraded is False


def test_T2_allow_above_ceiling_downgrades_to_ask(tmp_path: Path) -> None:
    store = _store(tmp_path, [_rule(RuleDecision.ALLOW, action_types=frozenset({"post"}))])
    for tier in (RiskTier.PUBLIC_CONTENT_IRREVERSIBLE, RiskTier.PUBLIC_AMPLIFYING_REVERSIBLE):
        m = store.match(action_type="post", risk_tier=tier, target_id="t", actor="a")
        assert m is not None, tier
        assert m.decision is RuleDecision.ASK, tier
        assert m.ceiling_downgraded is True, tier


def test_ceiling_cannot_be_bypassed_by_risk_tier_scoped_allow(tmp_path: Path) -> None:
    """A rule that explicitly scopes its ALLOW to an above-ceiling tier still
    downgrades — the ceiling is enforced at match time, not trusted from
    store content."""
    store = _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"post"}),
              risk_tiers=frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE})),
    ])
    m = store.match(action_type="post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.ASK


# ---------------------------------------------------------------------------
# M8-T3/T4/T5: precedence and the no-match default
# ---------------------------------------------------------------------------

def test_T3_never_beats_allow(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"delete_post"}), rule_id="allow-d"),
        _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="never-d"),
    ])
    m = store.match(action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.NEVER and m.rule_id == "never-d"


def test_T4_ask_beats_allow(tmp_path: Path) -> None:
    store = _store(tmp_path, [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="allow-b"),
        _rule(RuleDecision.ASK, action_types=frozenset({"bookmark"}), rule_id="ask-b"),
    ])
    m = store.match(action_type="bookmark", risk_tier=RiskTier.PRIVATE_REVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.ASK and m.rule_id == "ask-b"


def test_T5_no_match_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path, [_rule(RuleDecision.ALLOW, action_types=frozenset({"like"}))])
    assert store.match(action_type="post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                       target_id="t", actor="a") is None


# ---------------------------------------------------------------------------
# M8-T6: TTL
# ---------------------------------------------------------------------------

def test_T6_expired_rule_does_not_match(tmp_path: Path) -> None:
    _store(tmp_path, [_rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}),
                            ttl=10.0)])
    fresh = RuleStore(tmp_path / "rules.json", clock=lambda: 1005.0)
    m = fresh.match(action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                    target_id="t", actor="a")
    assert m is not None and m.decision is RuleDecision.NEVER

    gone = RuleStore(tmp_path / "rules.json", clock=lambda: 1011.0)
    assert gone.match(action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                      target_id="t", actor="a") is None


# ---------------------------------------------------------------------------
# M8-T7: failure semantics — zero rules, everything asks
# ---------------------------------------------------------------------------

def test_T7a_missing_store_is_empty(tmp_path: Path) -> None:
    assert RuleStore(tmp_path / "absent.json").load() == []


def test_T7b_corrupt_store_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_text("{not json", encoding="utf-8")
    assert RuleStore(p).load() == []


def test_T7c_schema_mismatch_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"schema_version": 999, "rules": []}), encoding="utf-8")
    assert RuleStore(p).load() == []


def test_T7d_one_invalid_entry_does_not_disable_the_rest(tmp_path: Path) -> None:
    """A corrupt entry is skipped; a valid NEVER rule in the same file still
    fires. One bad rule must never silently disable the owner's bans."""
    _store(tmp_path, [
        _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="good-never"),
    ])
    raw = json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
    raw["rules"].insert(0, {"rule_id": "broken", "decision": "wat", "selector": {}})
    (tmp_path / "rules.json").write_text(json.dumps(raw), encoding="utf-8")

    reloaded = RuleStore(tmp_path / "rules.json", clock=lambda: 1000.0)
    m = reloaded.match(action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
                       target_id="t", actor="a")
    assert m is not None and m.rule_id == "good-never"


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_save_load_round_trip_preserves_every_field(tmp_path: Path) -> None:
    rules = [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark", "like"}),
              target_ids=frozenset({"42"}), rule_id="rt-1", provenance="compiled",
              source_text="bookmarks are fine"),
        _rule(RuleDecision.NEVER, risk_tiers=frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE}),
              rule_id="rt-2"),
    ]
    store = _store(tmp_path, rules)
    loaded = {r.rule_id: r for r in store.load(force=True)}
    assert set(loaded) == {"rt-1", "rt-2"}
    r1 = loaded["rt-1"]
    assert r1.decision is RuleDecision.ALLOW and r1.provenance == "compiled"
    assert r1.source_text == "bookmarks are fine"
    assert r1.selector.action_types == frozenset({"bookmark", "like"})
    assert r1.selector.target_ids == frozenset({"42"})
    r2 = loaded["rt-2"]
    assert r2.selector.risk_tiers == frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE})


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    _store(tmp_path, [_rule(RuleDecision.ALLOW, action_types=frozenset({"like"}))])
    assert (tmp_path / "rules.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    # The saved file is valid JSON.
    json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
