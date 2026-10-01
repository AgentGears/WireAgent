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
from webwire.safety.user_rules import RuleStore

NOW = 2000.0


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


def _compiler(model) -> RuleCompiler:
    return RuleCompiler(model, registry=DEFAULT_REGISTRY, clock=lambda: NOW)


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
    assert d1.description == "NEVER when actions like — expires in 7.0 days"
    assert "please" not in d1.description and "stop" not in d1.description


async def test_double_confirm_is_an_error_not_a_duplicate(tmp_path: Path) -> None:
    model = _FakeModel(json.dumps({"decision": "allow", "action_types": ["like"]}))
    compiler = _compiler(model)
    draft = await compiler.compile("allow likes")
    store = _store(tmp_path)
    compiler.confirm(draft, store)
    with pytest.raises(RuleCompileError, match="already confirmed"):
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
    store, the kernel, and the dispatcher must never pull this module in."""
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "import webwire.safety.user_rules\n"
        "import webwire.safety.write_kernel\n"
        "import webwire.dispatcher\n"
        "assert 'webwire.safety.m8_compiler' not in sys.modules, "
        "'compiler leaked into the enforcement path'\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
