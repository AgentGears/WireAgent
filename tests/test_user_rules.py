"""M8 layer 1 tests — the standing-decision store (frozen spec sections 4-5).

Acceptance targets M8-T1 through M8-T9 from docs/M8_DESIGN.md, plus the
review-required regressions from the PR #20 fallback review (F-01..F-07):
whole-read invalidation, revocation freshness, finite TTLs, raised save
failures, strict parsing, id uniqueness, and the frozen matcher signature.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from webwire.safety.models import RiskTier
from webwire.safety.user_rules import (
    ALLOW_CEILING_TIERS,
    DEFAULT_RULE_TTL_S,
    RuleDecision,
    RuleSelector,
    RuleStore,
    RuleStoreError,
    UserRule,
)


def _rule(
    decision: RuleDecision,
    *,
    action_types: frozenset[str] | None = None,
    risk_tiers: frozenset[RiskTier] | None = None,
    target_types: frozenset[str] | None = None,
    target_ids: frozenset[str] | None = None,
    actors: frozenset[str] | None = None,
    rule_id: str | None = None,
    ttl: float = DEFAULT_RULE_TTL_S,
    now: float = 1000.0,
    provenance: str = "hand_written",
    source_text: str = "test rule",
) -> UserRule:
    return UserRule(
        rule_id=rule_id or f"r-{decision.value}",
        decision=decision,
        created_at=now,
        expires_at=now + ttl,
        provenance=provenance,
        source_text=source_text,
        selector=RuleSelector(
            action_types=action_types, risk_tiers=risk_tiers,
            target_types=target_types, target_ids=target_ids, actors=actors,
        ),
    )


def _m(
    store: RuleStore,
    *,
    action_type: str = "bookmark",
    risk_tier: RiskTier = RiskTier.PRIVATE_REVERSIBLE,
    target_type: str = "post",
    target_id: str = "t",
    actor: str = "a",
):
    return store.match(
        action_type=action_type, risk_tier=risk_tier, target_type=target_type,
        target_id=target_id, actor=actor,
    )


def _write(path: Path, rules: list[UserRule]) -> RuleStore:
    RuleStore(path, clock=lambda: 1000.0).save(rules)
    return RuleStore(path, clock=lambda: 1000.0)


# ---------------------------------------------------------------------------
# Creation invariants (M8-T9, F-03, F-07)
# ---------------------------------------------------------------------------

def test_empty_selector_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="must name its scope"):
        RuleSelector()


def test_invalid_decision_provenance_and_id_rejected() -> None:
    good = RuleSelector(action_types=frozenset({"like"}))
    with pytest.raises(ValueError):
        UserRule(rule_id="x", decision="maybe",  # type: ignore[arg-type]
                 created_at=0, expires_at=1, selector=good)
    with pytest.raises(ValueError):
        UserRule(rule_id="x", decision=RuleDecision.ALLOW, provenance="guessed",
                 created_at=0, expires_at=1, selector=good)
    with pytest.raises(ValueError, match="non-empty"):
        UserRule(rule_id="  ", decision=RuleDecision.ALLOW,
                 created_at=0, expires_at=1, selector=good)


@pytest.mark.parametrize("bad_created,bad_expires", [
    (float("nan"), 10.0), (10.0, float("nan")),
    (float("inf"), float("inf")), (float("-inf"), 10.0),
    (10.0, float("inf")),
    (10.0, 10.0),   # equal: zero TTL is not a TTL
    (20.0, 10.0),   # reversed
])
def test_F03_non_finite_or_nonforward_ttl_rejected(bad_created: float, bad_expires: float) -> None:
    with pytest.raises(ValueError):
        UserRule(rule_id="x", decision=RuleDecision.ALLOW,
                 created_at=bad_created, expires_at=bad_expires,
                 selector=RuleSelector(action_types=frozenset({"like"})))


def test_create_factory_applies_default_ttl() -> None:
    rule = UserRule.create(
        selector=RuleSelector(action_types=frozenset({"bookmark"})),
        decision=RuleDecision.ALLOW, rule_id="factory",
        source_text="bookmarks are fine", now=100.0,
    )
    assert rule.expires_at == 100.0 + DEFAULT_RULE_TTL_S
    assert rule.provenance == "hand_written"


# ---------------------------------------------------------------------------
# Dimension matching (M8-T8) + the frozen signature (F-06)
# ---------------------------------------------------------------------------

def test_F06_frozen_matcher_signature_with_target_type(tmp_path: Path) -> None:
    """The frozen M8_DESIGN.md section 4 signature — target_type included —
    is supported and test-locked."""
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.NEVER, target_types=frozenset({"user"}), rule_id="never-users"),
    ])
    hit = _m(store, action_type="follow", risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
             target_type="user", target_id="someone")
    assert hit is not None and hit.decision is RuleDecision.NEVER and hit.rule_id == "never-users"
    miss = _m(store, action_type="follow", risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
              target_type="post", target_id="123")
    assert miss is None


def test_selector_dimension_mismatch_no_match(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"})),
    ])
    assert _m(store, action_type="post",
              risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE) is None
    m = _m(store, action_type="delete_post",
           risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert m is not None and m.decision is RuleDecision.NEVER


def test_unspecified_dimensions_match_anything(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"})),
    ])
    for tier in RiskTier:
        assert _m(store, risk_tier=tier, target_id="anything", actor="anyone") is not None, tier


def test_target_scoped_never_beats_allow(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.NEVER, action_types=frozenset({"reply"}),
              target_ids=frozenset({"999"}), rule_id="never-999"),
        _rule(RuleDecision.ALLOW, action_types=frozenset({"reply"}), rule_id="allow-reply"),
    ])
    a = _m(store, action_type="reply", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
           target_id="999")
    assert a is not None and a.decision is RuleDecision.NEVER and a.rule_id == "never-999"
    b = _m(store, action_type="reply", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
           target_id="123")
    assert b is not None and b.decision is RuleDecision.ASK and b.ceiling_downgraded is True


# ---------------------------------------------------------------------------
# The ceiling (M8-T1/T2)
# ---------------------------------------------------------------------------

def test_T1_allow_below_ceiling_stands(tmp_path: Path) -> None:
    assert RiskTier.PRIVATE_REVERSIBLE in ALLOW_CEILING_TIERS
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"})),
    ])
    m = _m(store)
    assert m is not None and m.decision is RuleDecision.ALLOW and m.ceiling_downgraded is False


def test_T2_allow_above_ceiling_downgrades_to_ask(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"post"})),
    ])
    for tier in (RiskTier.PUBLIC_CONTENT_IRREVERSIBLE, RiskTier.PUBLIC_AMPLIFYING_REVERSIBLE):
        m = _m(store, action_type="post", risk_tier=tier)
        assert m is not None and m.decision is RuleDecision.ASK and m.ceiling_downgraded is True


def test_ceiling_cannot_be_bypassed_by_tier_scoped_allow(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"post"}),
              risk_tiers=frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE})),
    ])
    m = _m(store, action_type="post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert m is not None and m.decision is RuleDecision.ASK


# ---------------------------------------------------------------------------
# Precedence and the no-match default (M8-T3/T4/T5)
# ---------------------------------------------------------------------------

def test_T3_never_beats_allow(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"delete_post"}), rule_id="allow-d"),
        _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="never-d"),
    ])
    m = _m(store, action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert m is not None and m.decision is RuleDecision.NEVER and m.rule_id == "never-d"


def test_T4_ask_beats_allow(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="allow-b"),
        _rule(RuleDecision.ASK, action_types=frozenset({"bookmark"}), rule_id="ask-b"),
    ])
    m = _m(store)
    assert m is not None and m.decision is RuleDecision.ASK and m.rule_id == "ask-b"


def test_T5_no_match_returns_none(tmp_path: Path) -> None:
    store = _write(tmp_path / "rules.json", [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"like"})),
    ])
    assert _m(store, action_type="post") is None


# ---------------------------------------------------------------------------
# TTL (M8-T6)
# ---------------------------------------------------------------------------

def test_T6_expired_rule_does_not_match(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), ttl=10.0)])
    fresh = RuleStore(p, clock=lambda: 1005.0)
    m = _m(fresh, action_type="delete_post", risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert m is not None and m.decision is RuleDecision.NEVER
    gone = RuleStore(p, clock=lambda: 1011.0)
    assert _m(gone, action_type="delete_post",
              risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE) is None


# ---------------------------------------------------------------------------
# Failure semantics (M8-T7, F-01) — zero rules, everything asks
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


def test_F01_invalid_never_plus_valid_allow_voids_whole_read(tmp_path: Path) -> None:
    """THE widening case: a malformed restrictive NEVER entry must NOT leave
    a permissive ALLOW live. Any invalid entry → zero rules → the gate asks
    (pre-M8 behavior), never auto-approves."""
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="allow")])
    raw = json.loads(p.read_text(encoding="utf-8"))
    # Malformed NEVER: missing mandatory provenance (F-05 also fires).
    raw["rules"].insert(0, {
        "rule_id": "broken-never", "decision": "never",
        "created_at": 1000.0, "expires_at": 2000.0,
        "selector": {"action_types": ["bookmark"]},
    })
    p.write_text(json.dumps(raw), encoding="utf-8")

    store = RuleStore(p, clock=lambda: 1000.0)
    assert store.load() == []
    assert _m(store) is None, "no auto-approval past a broken ban"


def test_F01_invalid_allow_plus_valid_never_also_voids(tmp_path: Path) -> None:
    """The conservative direction: even a broken PERMISSIVE entry voids the
    read — the valid NEVER stops firing too (everything asks). Conservative
    in both directions, simple to reason about."""
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="n")])
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["rules"].insert(0, {"rule_id": "broken", "decision": "allow", "selector": {}})
    p.write_text(json.dumps(raw), encoding="utf-8")

    store = RuleStore(p, clock=lambda: 1000.0)
    assert store.load() == []
    assert _m(store, action_type="delete_post",
              risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE) is None


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(decision="wat"),                       # bad decision
    lambda r: r.pop("provenance"),                            # F-05: mandatory
    lambda r: r.pop("source_text"),                           # F-05: mandatory
    lambda r: r.update(created_at=float("nan")),              # F-03 via JSON literal
    lambda r: r.update(expires_at=float("inf")),
    lambda r: r.update(expires_at=r["created_at"]),           # equal
    lambda r: r.update(expires_at=0, created_at=10),          # reversed
    lambda r: r.update(rule_id=""),                           # F-07
    lambda r: r.update(rule_id=7),                            # wrong type
    lambda r: r.update(selector={"action_types": "bookmark"}),  # not a list
    lambda r: r.update(selector={}),                          # empty scope
])
def test_F01_F03_F05_strict_parse_matrix(tmp_path: Path, mutate) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="ok")])
    raw = json.loads(p.read_text(encoding="utf-8"))
    mutate(raw["rules"][0])
    p.write_text(json.dumps(raw), encoding="utf-8")
    store = RuleStore(p, clock=lambda: 1000.0)
    assert store.load() == [], mutate
    assert _m(store) is None, mutate


def test_F07_duplicate_ids_in_file_void_read(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="dup")])
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["rules"].append(dict(raw["rules"][0]))
    p.write_text(json.dumps(raw), encoding="utf-8")
    store = RuleStore(p, clock=lambda: 1000.0)
    assert store.load() == []


def test_F07_save_refuses_duplicate_ids(tmp_path: Path) -> None:
    a = _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="dup")
    b = _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="dup")
    with pytest.raises(RuleStoreError, match="duplicate"):
        RuleStore(tmp_path / "rules.json").save([a, b])


# ---------------------------------------------------------------------------
# Revocation freshness (F-02) — the long-lived store sees disk changes
# ---------------------------------------------------------------------------

def test_F02_revoked_allow_not_served_from_cache(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="a")])
    long_lived = RuleStore(p, clock=lambda: 1000.0)
    assert _m(long_lived) is not None, "baseline"

    # Another process (or the future CLI) rewrites the store without the rule.
    RuleStore(p, clock=lambda: 1000.0).save([])
    assert _m(long_lived) is None, "revoked ALLOW must not survive"


def test_F02_newly_added_never_seen_without_cache(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="a")])
    long_lived = RuleStore(p, clock=lambda: 1000.0)
    assert _m(long_lived).decision is RuleDecision.ALLOW

    RuleStore(p, clock=lambda: 1000.0).save([
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="a"),
        _rule(RuleDecision.NEVER, action_types=frozenset({"bookmark"}), rule_id="new-never"),
    ])
    m = _m(long_lived)
    assert m is not None and m.decision is RuleDecision.NEVER


# ---------------------------------------------------------------------------
# Persistence (F-04, round-trip, atomicity)
# ---------------------------------------------------------------------------

def test_F04_save_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "rules.json"
    _write(p, [_rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="n")])

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", _boom)
    with pytest.raises(RuleStoreError, match="save failed"):
        RuleStore(p).save([_rule(RuleDecision.NEVER,
                                 action_types=frozenset({"delete_post"}), rule_id="n")])
    # No temp litter from the failed write.
    assert not list(tmp_path.glob("*.tmp"))


def test_save_load_round_trip_preserves_every_field(tmp_path: Path) -> None:
    rules = [
        _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark", "like"}),
              target_types=frozenset({"post"}), target_ids=frozenset({"42"}),
              rule_id="rt-1", provenance="compiled", source_text="bookmarks are fine"),
        _rule(RuleDecision.NEVER, risk_tiers=frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE}),
              rule_id="rt-2"),
    ]
    store = _write(tmp_path / "rules.json", rules)
    loaded = {r.rule_id: r for r in store.load()}
    assert set(loaded) == {"rt-1", "rt-2"}
    r1 = loaded["rt-1"]
    assert r1.decision is RuleDecision.ALLOW and r1.provenance == "compiled"
    assert r1.source_text == "bookmarks are fine"
    assert r1.selector.action_types == frozenset({"bookmark", "like"})
    assert r1.selector.target_types == frozenset({"post"})
    assert r1.selector.target_ids == frozenset({"42"})
    r2 = loaded["rt-2"]
    assert r2.selector.risk_tiers == frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE})


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    _write(tmp_path / "rules.json", [_rule(RuleDecision.ALLOW, action_types=frozenset({"like"}))])
    assert (tmp_path / "rules.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# F-08: concurrent saves own unique staging files
# ---------------------------------------------------------------------------

def test_F08_concurrent_saves_no_cross_contamination(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """F-08's locked property under the F-19 serialization: every successful
    os.replace installs exactly that CALLER'S payload — with a shared staging
    file, writer A's replace installs writer B's bytes while A reports
    success. The original forced-simultaneity barrier (both writers stage
    before either replaces) is no longer reachable through save(): F-19 gave
    same-path mutations a process-wide lock, so concurrent saves serialize —
    a strictly stronger guarantee. Unique staging remains load-bearing (and
    is still asserted): each install carries its own writer's bytes, both
    saves succeed, the final file holds exactly one complete payload, and no
    staging litter remains."""
    import threading

    import webwire.safety.user_rules as ur_mod

    p = tmp_path / "rules.json"
    marker = threading.local()
    real_replace = os.replace
    captured: list[tuple[str, bytes]] = []
    lock = threading.Lock()

    def slow_replace(src, dst):
        with open(src, "rb") as f:
            data = f.read()
        # Capture only the FIRST attempt per thread — the Windows retry
        # helper re-enters os.replace on transient sharing violations, and
        # those retries must pass straight through.
        if not getattr(marker, "entered", False):
            marker.entered = True
            with lock:
                captured.append((marker.owner, data))
        return real_replace(src, dst)

    monkeypatch.setattr(ur_mod.os, "replace", slow_replace)

    never = _rule(RuleDecision.NEVER, action_types=frozenset({"delete_post"}), rule_id="never-x")
    allow = _rule(RuleDecision.ALLOW, action_types=frozenset({"bookmark"}), rule_id="allow-x")
    errors: list[Exception] = []

    def worker(owner: str, rule: UserRule) -> None:
        marker.owner = owner
        try:
            RuleStore(p).save([rule])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("never-writer", never))
    t2 = threading.Thread(target=worker, args=("allow-writer", allow))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"both saves must succeed; got {errors!r}"
    assert len(captured) == 2
    for owner, data in captured:
        rule_id = json.loads(data.decode("utf-8"))["rules"][0]["rule_id"]
        expected = "never-x" if owner == "never-writer" else "allow-x"
        assert rule_id == expected, (
            f"{owner} installed {rule_id!r} — cross-contaminated staging"
        )
    # Final file holds exactly one writer's complete payload; no litter.
    final = json.loads(p.read_text(encoding="utf-8"))
    assert len(final["rules"]) == 1
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Hardening: selector runtime contracts + bool parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "bookmark",                    # plain string
    {"bookmark"},                  # mutable set
    frozenset(),                   # empty: a dimension that matches nothing
    frozenset({"bookmark", ""}),   # empty-string element
    frozenset([7]),
])
def test_selector_dimensions_must_be_exact_frozensets(bad) -> None:
    with pytest.raises(ValueError):
        RuleSelector(action_types=bad)


def test_selector_risk_tiers_strict_type() -> None:
    with pytest.raises(ValueError):
        RuleSelector(risk_tiers=frozenset({"private_reversible"}))  # str, not enum
    with pytest.raises(ValueError):
        RuleSelector(risk_tiers={"private_reversible"})  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_created,bad_expires", [(True, 2.0), (1.0, True), (True, True)])
def test_bool_timestamps_rejected_at_construction(bad_created: float, bad_expires: float) -> None:
    """Parity with the persisted parser: bool is numeric in Python, so
    isfinite(True) passes — construction must reject what parsing rejects."""
    with pytest.raises(ValueError, match="must be a number"):
        UserRule(rule_id="b", decision=RuleDecision.ALLOW,
                 created_at=bad_created, expires_at=bad_expires,
                 selector=RuleSelector(action_types=frozenset({"x"})))



# ---------------------------------------------------------------------------
# F-25/F-26/F-29 (PR #23 second-pass review): the filesystem policy authority
# is shared across processes — and its fail-safe must survive invalid bytes.
# ---------------------------------------------------------------------------


class _assert_no_raise:
    """Context manager that fails the test if ANY exception escapes."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is None, f"load() leaked {exc_type.__name__}: {exc}"
        return True


def test_F26_invalid_utf8_is_fail_safe_everywhere(tmp_path: Path) -> None:
    """Actual invalid BYTES (not malformed-but-valid JSON): the enforcement
    reader voids to zero rules without raising; the strict mutation reader
    refuses with bytes unchanged."""
    p = tmp_path / "rules.json"
    raw = b'\xff\xfe{"rules": []}'  # BOM-ish garbage — not decodable UTF-8
    p.write_bytes(raw)
    store = RuleStore(p)

    with _assert_no_raise():
        assert store.load() == [], "invalid bytes must mean zero rules, no raise"

    with pytest.raises(RuleStoreError, match="refusing mutation"):
        store.append_strict(
            _rule(RuleDecision.ALLOW, action_types=frozenset({"like"}), rule_id="a1")
        )
    assert p.read_bytes() == raw, "a refused mutation must not touch a byte"


def test_F29_unique_per_save_staging_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-08's actual invariant, locked directly: every save stages through
    its OWN unique file. The serialized-save concurrency test can no longer
    catch a regression back to a shared 'rules.json.tmp' (the mutation fence
    serializes same-path writers), so the staging path shape is asserted
    here instead."""
    import webwire.safety.user_rules as ur_mod

    p = tmp_path / "rules.json"
    real_replace = os.replace
    staged: list[str] = []

    def capture_replace(src, dst):
        staged.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(ur_mod.os, "replace", capture_replace)
    store = RuleStore(p)
    store.save([_rule(RuleDecision.NEVER, action_types=frozenset({"like"}), rule_id="n1")])
    store.save([_rule(RuleDecision.ALLOW, action_types=frozenset({"like"}), rule_id="a1")])

    unique = list(dict.fromkeys(staged))  # the Windows retry helper may re-enter
    assert len(unique) == 2, staged
    for name in unique:
        assert name != "rules.json.tmp", "regressed to a shared staging file"
        assert name.startswith("rules.json.") and name.endswith(".tmp")
        uuid_part = name[len("rules.json.") : -len(".tmp")]
        assert len(uuid_part) == 32 and all(c in "0123456789abcdef" for c in uuid_part)
    assert not list(tmp_path.glob("*.tmp"))


_CHILD_APPEND = """
import sys
import time
from pathlib import Path

import webwire.safety.user_rules as ur
from webwire.safety.user_rules import (
    RuleDecision,
    RuleSelector,
    RuleStore,
    UserRule,
)

path, rule_id, mode = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "slow":
    original = RuleStore._load_strict

    def slowed(self):
        result = original(self)
        time.sleep(1.5)  # hold this append's read-modify-write window open
        return result

    RuleStore._load_strict = slowed

RuleStore(Path(path)).append_strict(
    UserRule(
        rule_id=rule_id,
        decision=RuleDecision.ALLOW,
        created_at=1.0,
        expires_at=9999999999.0,
        selector=RuleSelector(action_types=frozenset({"like"})),
        source_text="child process",
    )
)
print("OK", rule_id)
"""


def test_F25_two_processes_never_lose_an_append(tmp_path: Path) -> None:
    """The lost-update window across PROCESS boundaries: process A holds its
    append window open (slow strict load); process B appends inside that
    window. Without the interprocess fence, B's NEVER/ALLOW lands and A's
    stale replace silently drops it; with it, B blocks until A completes.
    Both rules must survive — two REAL processes, not threads."""
    import subprocess
    import sys

    p = tmp_path / "rules.json"

    # The child inherits the parent's exact sys.path: importing webwire.safety
    # pulls the package __init__ → dispatcher → the browser dependency, which
    # resolves wherever the TEST process found it (bare PYTHONPATH=src misses
    # it and fails on CI).
    parent_path = [os.path.abspath(entry) for entry in sys.path if entry]
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        parent_path + ([inherited] if inherited else [])
    )

    a = subprocess.Popen(
        [sys.executable, "-c", _CHILD_APPEND, str(p), "rule-a", "slow"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    b = subprocess.Popen(  # launched inside A's 1.5s window
        [sys.executable, "-c", _CHILD_APPEND, str(p), "rule-b", "fast"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    out_a, err_a = a.communicate(timeout=60)
    out_b, err_b = b.communicate(timeout=60)
    assert a.returncode == 0, err_a
    assert b.returncode == 0, err_b
    assert "OK" in out_a and "OK" in out_b

    ids = {r.rule_id for r in RuleStore(p).load()}
    assert ids == {"rule-a", "rule-b"}, (
        f"both concurrent cross-process appends must survive; saw {ids}"
    )
