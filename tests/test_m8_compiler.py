"""M8 layer 3 acceptance tests — the rule compiler (frozen spec section 7).

The safety-critical properties under test:

- The model never writes rules: strict deterministic reduction to the frozen
  selector contract; anything inexpressible is REJECTED with an explanation
  and NOTHING is stored (M8-T12).
- The owner confirms the COMPILATION: the description is re-generated from
  the structure by code, never echoed from the model's words; confirm() is
  the only call that persists.
- Compile-time only: a subprocess regression proves the enforcement path
  (store / kernel / dispatcher) never imports this compiler module.
- Pluggable + optional: a one-method model protocol; no model configured →
  unavailable, hand-written rules unaffected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from webwire.safety import DEFAULT_REGISTRY
from webwire.safety.m8_compiler import (
    ApiKeyChatModel,
    CompilerUnavailable,
    RuleCompileError,
    RuleCompiler,
    RuleCompileRejected,
    describe_compiled_rule,
    model_from_config,
)
from webwire.safety.models import RiskTier
from webwire.safety.user_rules import (
    RuleDecision,
    RuleSelector,
    RuleStore,
    RuleStoreError,
    UserRule,
)

NOW = 2000.0


class _Clock:
    """Mutable test clock: compile at one time, confirm at another (F-20)."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeModel:
    """Pluggable-model test double: returns a canned raw response string."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class _RaisingModel:
    async def complete(self, *, system: str, prompt: str) -> str:
        raise CompilerUnavailable("model endpoint failed: test")


def _compiler(model, clock=None) -> RuleCompiler:
    return RuleCompiler(
        model, registry=DEFAULT_REGISTRY, clock=clock or (lambda: NOW)
    )


def _store(tmp_path: Path) -> RuleStore:
    return RuleStore(tmp_path / "rules.json", clock=lambda: NOW)


async def test_T12_inexpressible_clause_stores_nothing(tmp_path: Path) -> None:
    """M8-T12: the model judges the clause inexpressible → rejected with the
    explanation surfaced verbatim; the store stays empty."""
    model = _FakeModel(
        json.dumps({"expressible": False, "explanation": "the clause names a mood, not an action"})
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("never let me post when I'm upset")
    assert exc.value.reason == "inexpressible"
    assert exc.value.explanation == "the clause names a mood, not an action"
    assert _store(tmp_path).load() == []


async def test_unknown_action_type_is_inexpressible(tmp_path: Path) -> None:
    """The compiled action must exist in the safety vocabulary — an unknown
    action can never match anything the kernel sends, which would be a
    silent no-op rule (fuzzification)."""
    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["vibe"]}))
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("allow vibing")
    assert exc.value.reason == "inexpressible"
    assert "vocabulary" in exc.value.explanation or "outside" in exc.value.explanation
    assert _store(tmp_path).load() == []


async def test_blanket_rule_is_inexpressible(tmp_path: Path) -> None:
    """The frozen selector contract requires a named scope: 'allow
    everything' has no exact expression."""
    model = _FakeModel(json.dumps({"decision": "allow"}))
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("allow everything")
    assert exc.value.reason == "inexpressible"
    assert "at least one" in exc.value.explanation


async def test_bad_decision_and_ttl_rejected(tmp_path: Path) -> None:
    for payload in (
        {"decision": "maybe", "action_types": ["like"]},
        {"decision": "allow", "action_types": ["like"], "ttl_seconds": 0},
        {"decision": "allow", "action_types": ["like"], "ttl_seconds": -5},
        {"decision": "allow", "action_types": ["like"], "ttl_seconds": 604801},
        {"decision": "allow", "action_types": ["like"], "ttl_seconds": True},
        {"decision": "allow", "action_types": []},
        {"decision": "allow", "action_types": "like"},
        {"decision": "allow", "risk_tiers": ["spooky"]},
    ):
        with pytest.raises(RuleCompileRejected) as exc:
            await _compiler(_FakeModel(json.dumps(payload))).compile("test clause")
        assert exc.value.reason in ("invalid_shape", "inexpressible")
        assert "nothing was stored" in exc.value.explanation
    assert _store(tmp_path).load() == []


async def test_unparseable_output_rejected_prose_not_fuzzed(tmp_path: Path) -> None:
    model = _FakeModel("Sure! I'd allow likes, reposts, and similar things.")
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("allow likes")
    assert exc.value.reason == "unparseable_response"
    # A fenced JSON object is the one mechanical tolerance; prose is not.
    fenced = _FakeModel("```json\n" + json.dumps({"decision": "never", "action_types": ["like"]}) + "\n```")
    draft = await _compiler(fenced).compile("never likes")
    assert draft.decision.value == "never"


async def test_unknown_fields_rejected_strict_shape(tmp_path: Path) -> None:
    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["like"], "confidence": 0.9}))
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("allow likes")
    assert exc.value.reason == "invalid_shape"
    assert "confidence" in exc.value.explanation


async def test_compile_stores_nothing_until_confirmed(tmp_path: Path) -> None:
    model = _FakeModel(json.dumps({"decision": "never", "action_types": ["like"]}))
    draft = await _compiler(model).compile("never let me like anything")
    assert draft.selector.action_types == frozenset({"like"})
    assert draft.decision.value == "never"
    assert draft.source_text == "never let me like anything"
    # Drafting alone persists nothing — the rules file does not even exist.
    assert not (tmp_path / "rules.json").exists()

    store = _store(tmp_path)
    rule = _compiler(model).confirm(draft, store)
    assert rule.provenance == "compiled"
    assert rule.source_text == "never let me like anything"
    loaded = store.load()
    assert [r.rule_id for r in loaded] == [draft.rule_id]
    # The matcher enforces it like any hand-written rule.
    match = store.match(
        action_type="like",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        target_type="post",
        target_id="1",
        actor="infaag",
    )
    assert match is not None and match.decision.value == "never"


async def test_custom_ttl_respected_and_capped_boundary(tmp_path: Path) -> None:
    exact_max = _FakeModel(json.dumps({"decision": "ask", "action_types": ["post"], "ttl_seconds": 604800}))
    draft = await _compiler(exact_max).compile("ask before posting")
    assert draft.ttl_seconds == 604800.0
    hour = _FakeModel(json.dumps({"decision": "ask", "action_types": ["post"], "ttl_seconds": 3600}))
    draft2 = await _compiler(hour).compile("ask before posting for an hour")
    store = _store(tmp_path)
    rule = _compiler(hour).confirm(draft2, store)
    assert rule.expires_at - rule.created_at == 3600.0


async def test_description_is_structural_not_model_words(tmp_path: Path) -> None:
    """Two different sentences compiling to the same structure produce the
    IDENTICAL description — it is generated from the selector, never echoed
    from the model's (or the owner's) words."""
    one = _FakeModel(json.dumps({"decision": "never", "action_types": ["like"]}))
    two = _FakeModel(json.dumps({"decision": "never", "action_types": ["like"]}))
    d1 = await _compiler(one).compile("stop me liking things, please")
    d2 = await _compiler(two).compile("no more likes ever again")
    assert d1.description == d2.description
    assert d1.description == describe_compiled_rule(d1.decision, d1.selector, d1.ttl_seconds)
    assert d1.description == (
        'NEVER when actions ["like"] — expires in 604800.0s (7.0 days)'
    )
    assert "please" not in d1.description and "stop" not in d1.description


async def test_double_confirm_is_an_error_not_a_duplicate(tmp_path: Path) -> None:
    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["like"]}))
    compiler = _compiler(model)
    draft = await compiler.compile("allow likes")
    store = _store(tmp_path)
    compiler.confirm(draft, store)
    with pytest.raises(RuleStoreError, match="duplicate"):
        compiler.confirm(draft, store)
    assert len(store.load()) == 1


async def test_confirm_preserves_existing_hand_written_rules(tmp_path: Path) -> None:
    from webwire.safety.user_rules import RuleDecision, RuleSelector, UserRule

    store = _store(tmp_path)
    store.save(
        [
            UserRule(
                rule_id="hand-1",
                decision=RuleDecision.NEVER,
                created_at=NOW,
                expires_at=NOW + 3600,
                selector=RuleSelector(action_types=frozenset({"bookmark"})),
                source_text="hand written",
            )
        ]
    )
    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["like"]}))
    compiler = _compiler(model)
    draft = await compiler.compile("allow likes")
    compiler.confirm(draft, store)
    ids = sorted(r.rule_id for r in store.load())
    assert ids == sorted(["hand-1", draft.rule_id])


async def test_compiled_rule_enforces_through_the_kernel_gate(tmp_path: Path) -> None:
    """A compiled+confirmed rule is an ordinary rule at enforcement time:
    the kernel NEVER branch denies a like citing the compiled rule id."""
    from webwire.config import WebWireConfig
    from webwire.journal import Journal
    from webwire.safety import (
        DedupeStore,
        KillSwitch,
        TokenBucket,
        WriteKernel,
    )
    from webwire.safety.models import PolicyVerdict

    store = _store(tmp_path)
    model = _FakeModel(json.dumps({"decision": "never", "action_types": ["like"]}))
    compiler = _compiler(model)
    draft = await compiler.compile("never let me like anything")
    compiler.confirm(draft, store)

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kernel = WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        rule_store=store,
    )

    class _Cap:
        name = "like_post"

        @property
        def tier(self):  # type: ignore[no-untyped-def]
            from webwire.capabilities.base import CapabilityTier

            return CapabilityTier.WRITE

        def compose(self, input, actor_identity):  # type: ignore[no-untyped-def]
            from webwire.safety import WriteIntent

            meta, comp = DEFAULT_REGISTRY.require("like")
            return WriteIntent(
                action_type="like",
                target_type="post",
                target_id="1",
                risk_meta=meta,
                compensation=comp,
                actor_identity="infaag",
            )

        async def preview(self, intent, broker):  # type: ignore[no-untyped-def]
            from webwire.safety.write_kernel import PreviewResult

            return PreviewResult(summary="Will like 1")

        async def execute(self, intent, broker):  # type: ignore[no-untyped-def]
            raise AssertionError("never-rule must stop before execute")

        async def verify(self, intent, broker):  # type: ignore[no-untyped-def]
            raise AssertionError("never-rule must stop before verify")

    r = await kernel.execute(_Cap(), object(), {"post_id": "1"})
    assert r.data["policy"]["verdict"] == PolicyVerdict.DENY.value
    assert r.data["policy"]["blocked_by"] == "user_rule"
    assert r.data["data"]["rule_id"] == draft.rule_id


async def test_no_model_configured_is_unavailable_handwritten_unaffected(
    tmp_path: Path,
) -> None:
    with pytest.raises(CompilerUnavailable):
        await _compiler(None).compile("allow likes")
    # Hand-written rules work with no model anywhere in the process.
    store = _store(tmp_path)
    assert store.load() == []


async def test_transport_failure_is_unavailable_not_rejection(tmp_path: Path) -> None:
    with pytest.raises(CompilerUnavailable):
        await _compiler(_RaisingModel()).compile("allow likes")


async def test_api_key_model_builds_the_expected_request(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_post_json(url, headers, payload, timeout_s):  # type: ignore[no-untyped-def]
        captured.update(url=url, headers=headers, payload=payload, timeout_s=timeout_s)
        return {"choices": [{"message": {"content": '{"expressible": false, "explanation": "no"}'}}]}

    model = ApiKeyChatModel(
        api_key=" test-key ",
        endpoint=" https://example.invalid/v1/chat ",
        model_name="m-test",
        timeout_s=5.0,
        post_json=fake_post_json,
    )
    content = await model.complete(system="sys", prompt="ask")
    assert json.loads(content)["expressible"] is False
    assert captured["url"] == "https://example.invalid/v1/chat"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["model"] == "m-test"
    assert captured["payload"]["temperature"] == 0.0
    assert [m["role"] for m in captured["payload"]["messages"]] == ["system", "user"]
    assert captured["timeout_s"] == 5.0


def test_model_from_config_requires_explicit_opt_in() -> None:
    from webwire.config import WebWireConfig

    assert model_from_config(WebWireConfig(state_dir=Path(".webwire"))) is None
    configured = WebWireConfig(
        state_dir=Path(".webwire"),
        compiler_api_key="key",
        compiler_endpoint="https://example.invalid/v1/chat",
        compiler_model_name="m-x",
    )
    model = model_from_config(configured)
    assert isinstance(model, ApiKeyChatModel)
    blank_key = WebWireConfig(
        state_dir=Path(".webwire"),
        compiler_api_key="   ",
        compiler_endpoint="https://example.invalid/v1/chat",
    )
    assert model_from_config(blank_key) is None


def test_enforcement_path_never_imports_the_compiler() -> None:
    """Frozen spec: no model executes during matching, gating, or approval.
    Runtime import-graph evidence from a clean interpreter: importing the
    store, the kernel, and the dispatcher must never pull this module in.

    The child inherits the parent's exact sys.path (webwire AND its browser
    dependency resolve wherever the test process found them — a bare
    PYTHONPATH=src misses conftest/dependency paths and fails on CI)."""
    code = (
        "import sys\n"
        "import webwire.safety.user_rules\n"
        "import webwire.safety.write_kernel\n"
        "import webwire.dispatcher\n"
        "assert 'webwire.safety.m8_compiler' not in sys.modules, "
        "'compiler leaked into the enforcement path'\n"
        "print('OK')\n"
    )
    parent_path = [os.path.abspath(p) for p in sys.path if p]
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        parent_path + ([inherited] if inherited else [])
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# F-19: strict, serialized store mutation — a store failure can never widen
# what rules permit
# ---------------------------------------------------------------------------


def _hand_rule(rule_id: str, decision: RuleDecision) -> UserRule:
    return UserRule(
        rule_id=rule_id,
        decision=decision,
        created_at=NOW,
        expires_at=NOW + 3600.0,
        selector=RuleSelector(action_types=frozenset({"like"})),
        source_text="hand written",
    )


async def test_F19_confirm_refuses_corrupt_store_bytes_unchanged(
    tmp_path: Path,
) -> None:
    """The widening sequence from the review: a real NEVER plus a malformed
    entry makes load() void the whole read (correct for enforcement).
    confirm() must REFUSE to mutate — computing []+new and saving would
    atomically erase the persisted NEVER and install the ALLOW."""
    store = _store(tmp_path)
    store.save([_hand_rule("never-keep", RuleDecision.NEVER)])
    p = tmp_path / "rules.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["rules"].append({"rule_id": "broken"})  # invalid entry → read voids
    p.write_text(json.dumps(doc), encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    compiler = _compiler(_FakeModel(json.dumps(
        {"decision": "allow", "action_types": ["like"]}
    )))
    draft = await compiler.compile("allow likes")
    with pytest.raises(RuleStoreError, match="refusing mutation"):
        compiler.confirm(draft, store)
    assert p.read_text(encoding="utf-8") == before, (
        "a refused mutation must not touch a single byte"
    )
    # The NEVER still cannot be erased by a later read-modify-write either.
    with pytest.raises(RuleStoreError, match="refusing mutation"):
        store.append_strict(_hand_rule("allow-2", RuleDecision.ALLOW))
    assert p.read_text(encoding="utf-8") == before


async def test_F19_missing_store_is_not_corrupt(tmp_path: Path) -> None:
    """Missing file = genuinely empty store (pre-M8 behavior) — mutation
    proceeds. Only corrupt/unreadable content refuses."""
    compiler = _compiler(_FakeModel(json.dumps(
        {"decision": "allow", "action_types": ["like"]}
    )))
    draft = await compiler.compile("allow likes")
    assert not (tmp_path / "rules.json").exists()
    compiler.confirm(draft, _store(tmp_path))
    assert [r.rule_id for r in _store(tmp_path).load()] == [draft.rule_id]


def test_F19_append_strict_serializes_same_path_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost-update window: while one writer holds the path lock
    mid-read-modify-write, another writer to the SAME path must block —
    an intervening NEVER can no longer be silently dropped by a stale
    base list."""
    import threading

    p = tmp_path / "rules.json"
    store_a, store_b = RuleStore(p), RuleStore(p)
    never = _hand_rule("never-1", RuleDecision.NEVER)
    store_a.save([never])

    entered, release = threading.Event(), threading.Event()
    original_load = RuleStore._load_strict

    def slow_load(self_inner: RuleStore) -> list[UserRule]:
        result = original_load(self_inner)
        if self_inner is store_a and not entered.is_set():
            entered.set()
            assert release.wait(5), "test barrier never released"
        return result

    monkeypatch.setattr(RuleStore, "_load_strict", slow_load)
    append_thread = threading.Thread(
        target=store_a.append_strict,
        args=(_hand_rule("allow-1", RuleDecision.ALLOW),),
    )
    append_thread.start()
    assert entered.wait(2), "append never reached its strict load"

    other_started, other_done = threading.Event(), threading.Event()

    def other_writer() -> None:
        other_started.set()
        store_b.save([never])
        other_done.set()

    writer_thread = threading.Thread(target=other_writer)
    writer_thread.start()
    assert other_started.wait(2)
    assert not other_done.wait(0.4), (
        "a same-path save must serialize behind the in-flight append"
    )
    release.set()
    append_thread.join(5)
    assert other_done.wait(5)
    assert not append_thread.is_alive() and not writer_thread.is_alive()
    # save() is a documented whole-store replacement: the later writer wins.
    assert {r.rule_id for r in RuleStore(p).load()} == {"never-1"}


# ---------------------------------------------------------------------------
# F-20: the rule's lifetime begins at confirmation
# ---------------------------------------------------------------------------


async def test_F20_ttl_starts_at_confirmation_not_compilation(
    tmp_path: Path,
) -> None:
    """A 60-second rule compiled at t=1000 and confirmed at t=5000 must be
    born at 5000 (alive until 5060) — not already-expired on arrival."""
    clock = _Clock(1000.0)
    compiler = RuleCompiler(
        _FakeModel(json.dumps(
            {"decision": "allow", "action_types": ["like"], "ttl_seconds": 60}
        )),
        registry=DEFAULT_REGISTRY,
        clock=clock,
    )
    draft = await compiler.compile("allow likes for a minute")
    assert draft.compiled_at == 1000.0

    clock.t = 5000.0  # the owner confirms much later
    rule = compiler.confirm(draft, _store(tmp_path))
    assert rule.created_at == 5000.0
    assert rule.expires_at == 5060.0
    assert not rule.is_expired(now=5059.0)
    assert rule.is_expired(now=5061.0)


# ---------------------------------------------------------------------------
# F-21: strict JSON and schema — no silent tolerance at the model boundary
# ---------------------------------------------------------------------------


async def test_F21_strict_json_rejects_ambiguity(tmp_path: Path) -> None:
    dup_keys = _FakeModel(
        '{"decision": "never", "decision": "allow", "action_types": ["like"]}'
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(dup_keys).compile("never likes")
    assert exc.value.reason == "unparseable_response"
    assert "duplicate" in exc.value.explanation

    nan_ttl = _FakeModel(
        '{"decision": "allow", "action_types": ["like"], "ttl_seconds": NaN}'
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(nan_ttl).compile("allow likes")
    assert exc.value.reason == "unparseable_response"
    assert "NaN" in exc.value.explanation

    # 1e999 parses to +inf WITHOUT tripping the literal-constant hook — the
    # finite check must catch it (the layer-1 non-finite-number lesson).
    huge_ttl = _FakeModel(
        '{"decision": "allow", "action_types": ["like"], "ttl_seconds": 1e999}'
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(huge_ttl).compile("allow likes")
    assert exc.value.reason == "inexpressible"
    assert "finite" in exc.value.explanation
    assert _store(tmp_path).load() == []


async def test_F21_malformed_refusal_shapes_never_become_rules(
    tmp_path: Path,
) -> None:
    """expressible must be a real JSON boolean: "false"/0/null are malformed
    drafts, not refusals, and must never reduce into a rule."""
    for raw in (
        '{"expressible": "false", "decision": "allow", "action_types": ["like"]}',
        '{"expressible": 0, "decision": "allow", "action_types": ["like"]}',
        '{"expressible": null, "decision": "allow", "action_types": ["like"]}',
        '{"expressible": false, "explanation": 5}',
        '{"expressible": false, "explanation": null}',
    ):
        with pytest.raises(RuleCompileRejected) as exc:
            await _compiler(_FakeModel(raw)).compile("some clause")
        assert exc.value.reason == "invalid_shape", raw
    assert _store(tmp_path).load() == []


async def test_F21_omitted_ttl_respects_custom_cap(tmp_path: Path) -> None:
    """An omitted ttl_seconds must not bypass a tighter configured maximum."""
    compiler = RuleCompiler(
        _FakeModel(json.dumps({"decision": "ask", "action_types": ["post"]})),
        registry=DEFAULT_REGISTRY,
        clock=lambda: NOW,
        max_ttl_seconds=3600,
    )
    draft = await compiler.compile("ask before posting")
    assert draft.ttl_seconds == 3600.0  # the cap, not the 7-day default


def test_F21_max_ttl_constructor_validated() -> None:
    for bad in (True, 0, -1, float("inf"), float("nan"), "x"):
        with pytest.raises(ValueError):
            RuleCompiler(
                None, registry=DEFAULT_REGISTRY, clock=lambda: NOW,
                max_ttl_seconds=bad,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# F-22: a compiled selector must be able to match a real action
# ---------------------------------------------------------------------------


async def test_F22_target_type_mismatch_is_inexpressible(tmp_path: Path) -> None:
    """like targets posts (registry vocabulary) — a never-like with
    target_types ["tweet"] can never match: a silent no-op ban."""
    model = _FakeModel(json.dumps(
        {"decision": "never", "action_types": ["like"], "target_types": ["tweet"]}
    ))
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("never like tweets")
    assert exc.value.reason == "inexpressible"
    assert "no registered action" in exc.value.explanation
    assert _store(tmp_path).load() == []


async def test_F22_tier_contradiction_is_inexpressible(tmp_path: Path) -> None:
    """like deterministically derives public_reversible_engagement — pairing
    it with private_reversible is an impossible conjunction."""
    model = _FakeModel(json.dumps(
        {"decision": "never", "action_types": ["like"],
         "risk_tiers": ["private_reversible"]}
    ))
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(model).compile("never like privately")
    assert exc.value.reason == "inexpressible"
    assert _store(tmp_path).load() == []


async def test_F22_satisfiable_conjunctions_compile(tmp_path: Path) -> None:
    for payload in (
        {"decision": "never", "action_types": ["like"], "target_types": ["post"]},
        {"decision": "ask", "action_types": ["post"], "target_types": ["none"]},
        {"decision": "allow", "action_types": ["like"],
         "risk_tiers": ["public_reversible_engagement"]},
        {"decision": "never", "target_types": ["post"]},
    ):
        draft = await _compiler(_FakeModel(json.dumps(payload))).compile("clause")
        assert draft.selector is not None


# ---------------------------------------------------------------------------
# F-23: the confirmation text is canonical, lossless, ceiling-aware — and
# confirm() revalidates it
# ---------------------------------------------------------------------------


def test_F23_description_is_lossless() -> None:
    """{"a","b"} and {"a or b"} must render differently; 86400s and 86401s
    must render differently — the owner confirms exact values."""
    sel_pair = RuleSelector(target_ids=frozenset({"a", "b"}))
    sel_single = RuleSelector(target_ids=frozenset({"a or b"}))
    d_pair = describe_compiled_rule(RuleDecision.NEVER, sel_pair, 3600.0)
    d_single = describe_compiled_rule(RuleDecision.NEVER, sel_single, 3600.0)
    assert d_pair != d_single
    assert '["a", "b"]' in d_pair
    assert '["a or b"]' in d_single

    d_day = describe_compiled_rule(RuleDecision.NEVER, sel_pair, 86400.0)
    d_day2 = describe_compiled_rule(RuleDecision.NEVER, sel_pair, 86401.0)
    assert d_day != d_day2
    assert "86400.0s" in d_day and "86401.0s" in d_day2


def test_F23_allow_descriptions_are_ceiling_aware() -> None:
    """An above-ceiling ALLOW is honestly reported as still asking."""
    like_only = RuleSelector(action_types=frozenset({"like"}))
    post_only = RuleSelector(action_types=frozenset({"post"}))
    assert "ASK" not in describe_compiled_rule(RuleDecision.ALLOW, like_only, 3600.0)

    post_desc = describe_compiled_rule(RuleDecision.ALLOW, post_only, 3600.0)
    assert "will still ASK" in post_desc
    assert "above the allow ceiling" in post_desc

    tier_above = RuleSelector(risk_tiers=frozenset({RiskTier.PUBLIC_CONTENT_IRREVERSIBLE}))
    assert "will still ASK" in describe_compiled_rule(
        RuleDecision.ALLOW, tier_above, 3600.0
    )
    # No action/tier dimension named: honestly conditional.
    actor_only = RuleSelector(actors=frozenset({"infaag"}))
    assert "actions above the allow ceiling will still ASK" in describe_compiled_rule(
        RuleDecision.ALLOW, actor_only, 3600.0
    )


async def test_F23_confirm_revalidates_manufactured_drafts(tmp_path: Path) -> None:
    """CompiledRuleDraft is public and constructible — confirm() must not
    trust it: misleading descriptions, out-of-contract TTLs, and non-enum
    decisions are refused before the store is touched."""
    from dataclasses import replace

    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["like"]}))
    compiler = _compiler(model)
    good = await compiler.compile("allow likes")

    lied = replace(good, description="ALLOW everything forever")
    with pytest.raises(RuleCompileError, match="canonical"):
        compiler.confirm(lied, _store(tmp_path))

    not_a_decision = replace(good, decision="allow")  # type: ignore[arg-type]
    with pytest.raises(RuleCompileError, match="decision"):
        compiler.confirm(not_a_decision, _store(tmp_path))

    capped = RuleCompiler(
        model, registry=DEFAULT_REGISTRY, clock=lambda: NOW, max_ttl_seconds=3600
    )
    capped_draft = await capped.compile("allow likes")
    over = replace(capped_draft, ttl_seconds=7200.0)
    with pytest.raises(RuleCompileError, match="ttl"):
        capped.confirm(over, _store(tmp_path))
    assert _store(tmp_path).load() == []


# ---------------------------------------------------------------------------
# F-24: the API key never appears in the config representation
# ---------------------------------------------------------------------------


def test_F24_api_key_absent_from_config_repr() -> None:
    from webwire.config import WebWireConfig

    cfg = WebWireConfig(
        state_dir=Path(".webwire"),
        compiler_api_key="hunter2-secret",
        compiler_endpoint="https://example.invalid/v1/chat",
    )
    assert "hunter2-secret" not in repr(cfg)
    assert cfg.compiler_api_key == "hunter2-secret"  # still readable by the owner


# ---------------------------------------------------------------------------
# F-27: the model output is TAGGED — refusal or rule, never both, never mixed
# ---------------------------------------------------------------------------


async def test_F27_tagged_schema_rejects_mixed_shapes(tmp_path: Path) -> None:
    """An explanation floating through rule fields, an expressible flag
    beside a decision, a refusal without its explanation — every mixed shape
    rejects as invalid_shape; only the two pure shapes interpret."""
    for raw in (
        # explanation floating through a rule (even null)
        '{"decision": "allow", "action_types": ["like"], "explanation": null}',
        '{"decision": "allow", "action_types": ["like"], "explanation": "why"}',
        # expressible flag beside rule fields
        '{"expressible": true, "decision": "allow", "action_types": ["like"], "explanation": 42}',
        '{"expressible": false, "decision": "never", "action_types": ["like"]}',
        # refusal without its explanation
        '{"expressible": false}',
        '{"expressible": false, "explanation": null}',
        '{"expressible": false, "explanation": ""}',
        # explanation with no refusal at all
        '{"explanation": "floating"}',
    ):
        with pytest.raises(RuleCompileRejected) as exc:
            await _compiler(_FakeModel(raw)).compile("some clause")
        assert exc.value.reason == "invalid_shape", raw
    assert _store(tmp_path).load() == []

    # The two PURE shapes still interpret exactly.
    refusal = _FakeModel(
        '{"expressible": false, "explanation": "cannot name an exact action"}'
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await _compiler(refusal).compile("vibe check my posts")
    assert exc.value.reason == "inexpressible"
    assert exc.value.explanation == "cannot name an exact action"
    draft = await _compiler(_FakeModel(
        json.dumps({"decision": "never", "action_types": ["like"]})
    )).compile("never likes")
    assert draft.decision.value == "never"


# ---------------------------------------------------------------------------
# F-28: unknown target vocabulary never establishes satisfiability
# ---------------------------------------------------------------------------


async def test_F28_unknown_target_vocabulary_never_proves_satisfiability(
    tmp_path: Path,
) -> None:
    """A custom action registered with the pre-Layer-3 constructor shape has
    target_types == () — UNKNOWN vocabulary. For a compiler whose rule is
    'express exactly or reject', unknown cannot establish satisfiability:
    naming that action plus any target type rejects."""
    from webwire.safety.models import (
        Amplification,
        CompensationMeta,
        Reversibility,
        RiskMeta,
        Visibility,
    )
    from webwire.safety.risk_registry import RiskRegistry

    custom = RiskRegistry()
    custom.register(
        "custom",
        RiskMeta(  # no target_types — the default () means unknown
            visibility=Visibility.PRIVATE,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.NONE,
        ),
        CompensationMeta(supports_compensation=False),
    )
    compiler = RuleCompiler(
        _FakeModel(json.dumps(
            {"decision": "never", "action_types": ["custom"],
             "target_types": ["spaceship"]}
        )),
        registry=custom,
        clock=lambda: NOW,
    )
    with pytest.raises(RuleCompileRejected) as exc:
        await compiler.compile("never custom the spaceship")
    assert exc.value.reason == "inexpressible"
    assert _store(tmp_path).load() == []

    # Naming the action WITHOUT a target constraint is still expressible —
    # the tier derives and no target claim is made.
    plain = RuleCompiler(
        _FakeModel(json.dumps({"decision": "ask", "action_types": ["custom"]})),
        registry=custom,
        clock=lambda: NOW,
    )
    draft = await plain.compile("ask before custom things")
    assert draft.selector.action_types == frozenset({"custom"})
