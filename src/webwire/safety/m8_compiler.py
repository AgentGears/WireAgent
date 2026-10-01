"""M8 user rule layer — the compiler (frozen spec: docs/M8_DESIGN.md,
section 7; layer 3 of the M8 build order).

Natural language in; a structured selector out; the compiled form shown back
to the owner in plain words for confirmation BEFORE storage. Clauses the
compiler cannot express deterministically are REJECTED with an explanation,
never fuzzified.

Safety properties load-bearing here (frozen spec + standing decisions):

- **The model never writes rules.** The model returns a JSON draft; strict
  deterministic validation reduces it to a ``RuleSelector`` + decision, and
  the description the owner confirms is re-generated FROM that structure by
  code, never echoed from the model's words.
- **Compile-time only.** Nothing in this module is imported by the store,
  the matcher, the kernel, or any enforcement path. A subprocess-level
  regression locks that import graph.
- **Optional and pluggable.** The model boundary is a one-method protocol;
  the built-in implementation is a plain API-key chat client. With no model
  configured, hand-written rules work exactly as before.
- **Ambiguity surfaces once, at creation.** ``compile()`` produces a draft
  and stores nothing; ``confirm()`` is the only call that persists, and the
  owner confirms the compilation (the re-expressed structure), not the words.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from webwire.safety.models import RiskTier
from webwire.safety.risk_registry import RiskRegistry
from webwire.safety.user_rules import (
    DEFAULT_RULE_TTL_S,
    RuleDecision,
    RuleSelector,
    RuleStore,
    UserRule,
    validate_rule_id,
)

__all__ = [
    "RuleCompileError",
    "RuleCompileRejected",
    "CompilerUnavailable",
    "CompilerModel",
    "ApiKeyChatModel",
    "model_from_config",
    "CompiledRuleDraft",
    "RuleCompiler",
    "describe_compiled_rule",
]

_KNOWN_DECISIONS = frozenset(d.value for d in RuleDecision)


class RuleCompileError(Exception):
    """Base for compiler failures. No failure path ever stores a rule."""


class RuleCompileRejected(RuleCompileError):
    """The clause cannot be expressed deterministically under the frozen
    rule contract. Nothing was stored; the explanation is for the owner."""

    def __init__(self, reason: str, explanation: str) -> None:
        self.reason = reason
        self.explanation = explanation
        super().__init__(f"compile rejected ({reason}): {explanation}")


class CompilerUnavailable(RuleCompileError):
    """Operational: no model is configured, or the model endpoint failed.
    Distinct from rejection — the clause was never semantically judged."""


@runtime_checkable
class CompilerModel(Protocol):
    """The pluggable model boundary: one string in, one string out."""

    async def complete(self, *, system: str, prompt: str) -> str: ...


_PostJson = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Default transport: a plain JSON POST via the standard library."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CompilerUnavailable(f"model endpoint failed: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompilerUnavailable(f"model endpoint returned non-JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CompilerUnavailable("model endpoint response was not a JSON object")
    return parsed


class ApiKeyChatModel:
    """The built-in pluggable model: a plain API key against a
    chat-completions-style endpoint, configured by the caller.

    Kept deliberately thin: no retries, no streaming, no vendor SDK. The
    transport is injectable so tests exercise the real request construction
    (URL, bearer header, body shape) without network access.
    """

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model_name: str = "default",
        timeout_s: float = 30.0,
        post_json: Optional[_PostJson] = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        self._api_key = api_key.strip()
        self._endpoint = endpoint.strip()
        self._model_name = model_name
        self._timeout_s = timeout_s
        self._post_json = post_json if post_json is not None else _http_post_json

    async def complete(self, *, system: str, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        parsed = await asyncio.to_thread(
            self._post_json, self._endpoint, headers, payload, self._timeout_s
        )
        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, TypeError, IndexError) as exc:
            raise CompilerUnavailable(
                f"model endpoint response lacked a completion: {exc}"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise CompilerUnavailable("model endpoint returned an empty completion")
        return content


def model_from_config(
    config: Any,
) -> Optional[ApiKeyChatModel]:
    """Build the built-in model from a WebWireConfig, or None when the
    caller has not opted in (no key / no endpoint — hand-written rules only)."""
    api_key = getattr(config, "compiler_api_key", None)
    endpoint = getattr(config, "compiler_endpoint", None)
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    return ApiKeyChatModel(
        api_key=api_key,
        endpoint=endpoint,
        model_name=getattr(config, "compiler_model_name", "default") or "default",
    )


@dataclass(frozen=True)
class CompiledRuleDraft:
    """The compile result: NOT a rule. Nothing is stored until the owner
    confirms — and the owner confirms ``description``, which code generated
    from the structure below, not from the model's words."""

    rule_id: str
    decision: RuleDecision
    selector: RuleSelector
    ttl_seconds: float
    source_text: str
    description: str
    compiled_at: float


def describe_compiled_rule(
    decision: RuleDecision,
    selector: RuleSelector,
    ttl_seconds: float,
) -> str:
    """Deterministically re-express a compiled selector in plain words.

    Generated only from the structure — two different source sentences that
    compile to the same selector produce the identical description (locked
    by regression). This is the text the owner actually confirms."""

    def dim(name: str, values: frozenset[Any]) -> str:
        return f"{name} {' or '.join(sorted(str(v) for v in values))}"

    parts: list[str] = []
    if selector.action_types is not None:
        parts.append(dim("actions", selector.action_types))
    if selector.risk_tiers is not None:
        parts.append(dim("risk tiers", selector.risk_tiers))
    if selector.target_types is not None:
        parts.append(dim("target types", selector.target_types))
    if selector.target_ids is not None:
        parts.append(dim("target ids", selector.target_ids))
    if selector.actors is not None:
        parts.append(dim("actors", selector.actors))
    days = ttl_seconds / 86400.0
    ttl = f"{days:.1f} days" if days >= 1.0 else f"{ttl_seconds:.0f} seconds"
    scope = "; ".join(parts) if parts else "nothing named"
    return f"{decision.value.upper()} when {scope} — expires in {ttl}"


_SYSTEM_PROMPT = (
    "You compile personal-policy sentences into strict JSON rule selectors "
    "for a safety kernel. Return ONLY a single JSON object — no prose, no "
    "code fences. If the sentence cannot be expressed EXACTLY and "
    "conservatively with the allowed vocabulary, return "
    '{"expressible": false, "explanation": "<why, one sentence>"}. '
    "Never guess a mapping, never broaden a scope, never invent vocabulary."
)

_ALLOWED_KEYS = frozenset(
    {
        "expressible",
        "explanation",
        "decision",
        "action_types",
        "risk_tiers",
        "target_types",
        "target_ids",
        "actors",
        "ttl_seconds",
    }
)


class RuleCompiler:
    """Compile-time bridge from natural language to the frozen rule store."""

    def __init__(
        self,
        model: Optional[CompilerModel],
        *,
        registry: RiskRegistry,
        clock: Callable[[], float],
        max_ttl_seconds: float = DEFAULT_RULE_TTL_S,
    ) -> None:
        if max_ttl_seconds <= 0:
            raise ValueError("max_ttl_seconds must be > 0")
        self._model = model
        self._registry = registry
        self._clock = clock
        self._max_ttl = max_ttl_seconds

    async def compile(self, text: str) -> CompiledRuleDraft:
        """Draft a rule from natural language. Stores NOTHING.

        Raises RuleCompileRejected when the clause is inexpressible under
        the frozen rule contract, and CompilerUnavailable when no model is
        configured or the endpoint failed operationally."""
        if not isinstance(text, str) or not text.strip():
            raise RuleCompileRejected("empty_clause", "the rule text is empty")
        if self._model is None:
            raise CompilerUnavailable("no compiler model is configured")

        known_actions = self._registry.known_actions()
        known_tiers = [t.value for t in RiskTier]
        prompt = "\n".join(
            [
                "Allowed vocabulary:",
                f"- decisions: {', '.join(sorted(_KNOWN_DECISIONS))}",
                f"- action_types: {', '.join(known_actions)}",
                f"- risk_tiers: {', '.join(known_tiers)}",
                (
                    "Fields (all optional except decision): expressible(bool), "
                    "explanation(str), decision(str), "
                    "action_types(list[str]), risk_tiers(list[str]), "
                    "target_types(list[str]), target_ids(list[str]), "
                    "actors(list[str]), ttl_seconds(number, at most "
                    f"{self._max_ttl:.0f}). Unspecified dimensions match "
                    "anything; a rule MUST name at least one dimension."
                ),
                "",
                f"Sentence: {text.strip()}",
            ]
        )
        raw = await self._model.complete(system=_SYSTEM_PROMPT, prompt=prompt)
        draft = self._reduce(text.strip(), raw)
        return draft

    # -- deterministic reduction ------------------------------------------

    def _reduce(self, source_text: str, raw: str) -> CompiledRuleDraft:
        payload = self._parse_json(raw)
        if payload.get("expressible") is False:
            explanation = payload.get("explanation")
            explanation = (
                explanation
                if isinstance(explanation, str) and explanation.strip()
                else "the model judged the clause inexpressible"
            )
            raise RuleCompileRejected("inexpressible", explanation)

        decision = self._reduce_decision(payload)
        selector = self._reduce_selector(payload)
        ttl = self._reduce_ttl(payload.get("ttl_seconds"))

        rule_id = f"compiled-{uuid.uuid4().hex[:12]}"
        validate_rule_id(rule_id)
        return CompiledRuleDraft(
            rule_id=rule_id,
            decision=decision,
            selector=selector,
            ttl_seconds=ttl,
            source_text=source_text,
            description=describe_compiled_rule(decision, selector, ttl),
            compiled_at=self._clock(),
        )

    def _parse_json(self, raw: str) -> dict[str, Any]:
        # One deterministic mechanical tolerance: strip a surrounding code
        # fence if present, then require strict JSON. Anything else the model
        # emitted is unparseable, not fuzzed into shape.
        text = raw.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1 :]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleCompileRejected(
                "unparseable_response",
                f"the model did not return JSON ({exc.msg}); nothing was stored",
            ) from exc
        if not isinstance(payload, dict):
            raise RuleCompileRejected(
                "unparseable_response",
                "the model returned JSON that is not an object; nothing was stored",
            )
        unknown = sorted(set(payload.keys()) - _ALLOWED_KEYS)
        if unknown:
            raise RuleCompileRejected(
                "invalid_shape",
                f"unknown fields {unknown} in the compiled draft; nothing was stored",
            )
        return payload

    def _reduce_decision(self, payload: dict[str, Any]) -> RuleDecision:
        value = payload.get("decision")
        if not isinstance(value, str) or value not in _KNOWN_DECISIONS:
            allowed = ", ".join(sorted(_KNOWN_DECISIONS))
            raise RuleCompileRejected(
                "invalid_shape",
                f"decision {value!r} is not one of {allowed}; nothing was stored",
            )
        return RuleDecision(value)

    def _reduce_selector(self, payload: dict[str, Any]) -> RuleSelector:
        dims: dict[str, Optional[frozenset[Any]]] = {
            "action_types": None,
            "risk_tiers": None,
            "target_types": None,
            "target_ids": None,
            "actors": None,
        }
        for name in ("action_types", "target_types", "target_ids", "actors"):
            dims[name] = self._string_dim(payload, name)
        dims["risk_tiers"] = self._tier_dim(payload)

        if all(v is None for v in dims.values()):
            raise RuleCompileRejected(
                "inexpressible",
                "blanket rules are not supported: a rule must name at least one "
                "dimension (action, risk tier, target, or actor); nothing was stored",
            )
        return RuleSelector(**dims)

    def _string_dim(
        self, payload: dict[str, Any], name: str
    ) -> Optional[frozenset[str]]:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise RuleCompileRejected(
                "invalid_shape",
                f"{name} must be a non-empty list of strings; nothing was stored",
            )
        entries: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise RuleCompileRejected(
                    "invalid_shape",
                    f"{name} entries must be non-empty strings; nothing was stored",
                )
            entries.append(item.strip())
        if name == "action_types":
            known = frozenset(self._registry.known_actions())
            unknown = sorted(set(entries) - known)
            if unknown:
                raise RuleCompileRejected(
                    "inexpressible",
                    f"action types {unknown} are outside the safety vocabulary; "
                    "nothing was stored",
                )
        return frozenset(entries)

    def _tier_dim(self, payload: dict[str, Any]) -> Optional[frozenset[RiskTier]]:
        value = payload.get("risk_tiers")
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise RuleCompileRejected(
                "invalid_shape",
                "risk_tiers must be a non-empty list of strings; nothing was stored",
            )
        tiers: list[RiskTier] = []
        for item in value:
            if not isinstance(item, str) or item not in RiskTier._value2member_map_:
                raise RuleCompileRejected(
                    "inexpressible",
                    f"risk tier {item!r} is outside the tier vocabulary; "
                    "nothing was stored",
                )
            tiers.append(RiskTier(item))
        return frozenset(tiers)

    def _reduce_ttl(self, value: Any) -> float:
        if value is None:
            return DEFAULT_RULE_TTL_S
        # bools are ints in Python (the layer-1 parse lesson): reject them
        # rather than letting True parse as 1 second.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleCompileRejected(
                "invalid_shape",
                "ttl_seconds must be a number; nothing was stored",
            )
        ttl = float(value)
        if ttl <= 0:
            raise RuleCompileRejected(
                "inexpressible",
                f"ttl {ttl} is not a positive duration; nothing was stored",
            )
        if ttl > self._max_ttl:
            raise RuleCompileRejected(
                "inexpressible",
                f"ttl exceeds the maximum {self._max_ttl:.0f}s — rules are "
                "short-lived by contract; nothing was stored",
            )
        return ttl

    # -- the confirm-compile surface ---------------------------------------

    def confirm(self, draft: CompiledRuleDraft, store: RuleStore) -> UserRule:
        """Persist one confirmed draft. The ONLY call that stores.

        The owner has confirmed ``draft.description`` — the code-generated
        re-expression of the compiled structure — not the source words.
        Confirmation is single-use: the rule_id guard makes a double
        confirm an error instead of a duplicate rule."""
        existing = store.load()
        if draft.rule_id in {r.rule_id for r in existing}:
            raise RuleCompileError(
                f"draft {draft.rule_id} is already confirmed; nothing stored"
            )
        rule = UserRule.create(
            selector=draft.selector,
            decision=draft.decision,
            rule_id=draft.rule_id,
            provenance="compiled",
            source_text=draft.source_text,
            ttl_seconds=draft.ttl_seconds,
            now=draft.compiled_at,
        )
        store.save(existing + [rule])
        return rule
