"""M8 user rule layer — the compiler (frozen spec: docs/M8_DESIGN.md,
section 7; layer 3 of the M8 build order).

Natural language in; a structured selector out; the compiled form shown back
to the owner in plain words for confirmation BEFORE storage. Clauses the
compiler cannot express deterministically are REJECTED with an explanation,
never fuzzified.

Safety properties load-bearing here (frozen spec + standing decisions):

- **The model never writes rules.** The model returns a JSON draft; strict
  deterministic validation (duplicate keys, non-finite numbers, malformed
  refusals — all rejected, F-21) reduces it to a ``RuleSelector`` +
  decision. The description the owner confirms is re-generated FROM that
  structure by code — canonical and lossless (F-23) — never echoed from the
  model's words.
- **Impossible rules are rejected, not stored.** A selector provably unable
  to match any registered action — wrong target type for the named action,
  contradictory action/tier conjunction — would be a silent no-op rule, so
  it is rejected against the registry's deterministic vocabulary (F-22).
- **Compile-time only.** Nothing in this module is imported by the store,
  the matcher, the kernel, or any enforcement path. A subprocess-level
  regression locks that import graph.
- **Optional and pluggable.** The model boundary is a one-method protocol;
  the built-in implementation is a plain API-key chat client. With no model
  configured, hand-written rules work exactly as before.
- **Ambiguity surfaces once, at creation.** ``compile()`` produces a draft
  and stores nothing; ``confirm()`` is the only call that persists. The
  owner confirms the COMPILATION (the canonical re-expression), the rule's
  lifetime begins at confirmation (F-20), and persistence goes through the
  store's strict serialized append (F-19): a corrupt store refuses mutation
  with bytes unchanged — a rule-store failure can never widen what rules
  permit.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from webwire.safety.models import RiskTier
from webwire.safety.risk_registry import DEFAULT_REGISTRY, RiskRegistry
from webwire.safety.user_rules import (
    ALLOW_CEILING_TIERS,
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
    from the structure below, not from the model's words.

    Public and constructible by design (layer 4 surfaces it); ``confirm()``
    therefore revalidates it against the compiler's own constraints instead
    of trusting it (F-23)."""

    rule_id: str
    decision: RuleDecision
    selector: RuleSelector
    ttl_seconds: float
    source_text: str
    description: str
    compiled_at: float


# -- strict JSON (F-21) ------------------------------------------------------

class _StrictJsonError(ValueError):
    """A strict-loader violation: duplicate keys or a non-standard constant."""


def _reject_constant(name: str) -> Any:
    raise _StrictJsonError(f"non-standard JSON constant {name!r}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _StrictJsonError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _loads_strict(text: str) -> Any:
    """json.loads with the two silent-tolerance holes closed: duplicate
    object keys (last-value-wins hides ambiguity) and NaN/Infinity
    literals (non-finite numbers must never reach a TTL)."""
    return json.loads(
        text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
    )


# -- the canonical owner-facing representation (F-23) ------------------------

def _friendly_duration(ttl_seconds: float) -> str:
    if ttl_seconds >= 86400.0:
        return f"{ttl_seconds / 86400.0:.1f} days"
    if ttl_seconds >= 3600.0:
        return f"{ttl_seconds / 3600.0:.1f} hours"
    if ttl_seconds >= 60.0:
        return f"{ttl_seconds / 60.0:.1f} minutes"
    return f"{ttl_seconds:.1f} seconds"


def _quoted_array(values: Any) -> str:
    """Sorted, individually quoted/escaped values — ["a", "b"] is never
    confusable with ["a or b"] (F-23: the confirmation text must uniquely
    represent the compiled rule)."""
    return "[" + ", ".join(json.dumps(str(v)) for v in sorted(values)) + "]"


def _ceiling_note(
    decision: RuleDecision,
    selector: RuleSelector,
    registry: RiskRegistry,
) -> str:
    """The honest effective-policy line (frozen spec: an above-ceiling ALLOW
    is reported as 'that will still ask'), derived from the same tier
    vocabulary the layer-2 gate enforces at match time.

    TOTAL over stored rules (F-35): an action outside the supplied registry
    (a custom, stale, or removed capability) renders an explicit
    unverifiable-ceiling note — a tier is never INFERRED for an unknown
    action, and management display never raises on legal rules. The
    compiler itself rejects unknown actions before rendering, so this path
    exists for lifecycle rendering of already-stored rules."""
    if decision is not RuleDecision.ALLOW:
        return ""
    # F-49: the action vocabulary is checked WHENEVER action_types is
    # present — independently of whether risk_tiers is also specified. A
    # mixed action+tier selector with an unknown action is exactly as
    # latent (currently non-executable, possibly live authority after a
    # future registration) as an action-only selector.
    unknown_actions = (
        [a for a in selector.action_types if registry.get(a) is None]
        if selector.action_types is not None
        else []
    )
    if unknown_actions:
        # F-55: compose the two facts — unknown action AND effective tier
        # ceiling — rather than choosing one. With explicit tiers, whether
        # auto-approval is even possible is DETERMINED by those tiers: all
        # above the ceiling means any future matching registration still
        # ASKs; below-ceiling tiers are the only auto-approval route.
        prefix = (
            "ceiling not verifiable: some named actions are outside "
            "the active registry — such actions are currently "
            "non-executable"
        )
        if selector.risk_tiers is None:
            # F-57: no explicit tiers — derive the KNOWN actions' registry
            # tiers and compose them with the unknown-action warning. A
            # rule like {post, future_action} must say BOTH: the unknown
            # action's latent behavior AND that post (above ceiling) will
            # still ASK — the unknown must not mask known behavior.
            latent = (
                f"{prefix}; such actions may auto-approve only if later "
                "registered below the standing-rule ceiling"
            )
            known_tiers = {
                registry.require(a)[0].derive_tier()
                for a in (selector.action_types or frozenset())
                if registry.get(a) is not None
            }
            if known_tiers - ALLOW_CEILING_TIERS:
                return (
                    f"{latent}; the known above-ceiling actions in this "
                    "rule will still ASK"
                )
            return latent
        below = selector.risk_tiers & ALLOW_CEILING_TIERS
        above = selector.risk_tiers - ALLOW_CEILING_TIERS
        if not below:
            return (
                f"{prefix}; if one is later registered at a tier matching "
                "this selector, it will still ASK (every named tier is "
                "above the allow ceiling)"
            )
        note = (
            f"{prefix}; it may auto-approve only if later registered at "
            "one of the below-ceiling tiers matching this selector"
        )
        if above:
            note += "; matching above-ceiling tiers still ASK"
        return note
    if selector.risk_tiers is not None:
        tiers = set(selector.risk_tiers)
    elif selector.action_types is not None:
        tiers = {
            registry.require(a)[0].derive_tier() for a in selector.action_types
        }
    else:
        return "actions above the allow ceiling will still ASK"
    if not tiers & ALLOW_CEILING_TIERS:
        return "will still ASK: every named tier is above the allow ceiling"
    if tiers - ALLOW_CEILING_TIERS:
        return "actions above the allow ceiling will still ASK"
    return ""


def describe_compiled_rule(
    decision: RuleDecision,
    selector: RuleSelector,
    ttl_seconds: float,
    *,
    registry: RiskRegistry = DEFAULT_REGISTRY,
) -> str:
    """The canonical, lossless owner-facing representation of a compiled
    rule (F-23).

    Generated only from the structure: sorted quoted/escaped arrays (so
    {"a","b"} and {"a or b"} render differently), the exact TTL seconds
    alongside a friendly duration (86400 and 86401 render differently), and
    explicit effective-ceiling semantics for ALLOW. Two structures produce
    the same description only if they are the same rule; the owner confirms
    THIS text, and confirm() re-derives it before persisting."""
    parts: list[str] = []
    if selector.action_types is not None:
        parts.append(f"actions {_quoted_array(selector.action_types)}")
    if selector.risk_tiers is not None:
        parts.append(f"risk tiers {_quoted_array(selector.risk_tiers)}")
    if selector.target_types is not None:
        parts.append(f"target types {_quoted_array(selector.target_types)}")
    if selector.target_ids is not None:
        parts.append(f"target ids {_quoted_array(selector.target_ids)}")
    if selector.actors is not None:
        parts.append(f"actors {_quoted_array(selector.actors)}")
    scope = "; ".join(parts) if parts else "nothing named"
    text = (
        f"{decision.value.upper()} when {scope} — expires in "
        f"{json.dumps(float(ttl_seconds))}s ({_friendly_duration(ttl_seconds)})"
    )
    note = _ceiling_note(decision, selector, registry)
    if note:
        text += f" — {note}"
    return text


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

_REFUSAL_FIELD_KEYS = ("expressible", "explanation")
_RULE_FIELD_KEYS = (
    "decision",
    "action_types",
    "risk_tiers",
    "target_types",
    "target_ids",
    "actors",
    "ttl_seconds",
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
        if (
            isinstance(max_ttl_seconds, bool)
            or not isinstance(max_ttl_seconds, (int, float))
            or not math.isfinite(max_ttl_seconds)
            or max_ttl_seconds <= 0
        ):
            raise ValueError("max_ttl_seconds must be a finite positive number")
        self._model = model
        self._registry = registry
        self._clock = clock
        self._max_ttl = float(max_ttl_seconds)

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
        target_vocab = ", ".join(
            f"{a}={sorted(self._registry.require(a)[0].target_types)}"
            for a in known_actions
            if self._registry.require(a)[0].target_types
        )
        prompt = "\n".join(
            [
                "Allowed vocabulary:",
                f"- decisions: {', '.join(sorted(_KNOWN_DECISIONS))}",
                f"- action_types: {', '.join(known_actions)}",
                f"- risk_tiers: {', '.join(known_tiers)}",
                f"- valid target types by action: {target_vocab}",
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
        return self._reduce(text.strip(), raw)

    # -- deterministic reduction ------------------------------------------

    def _reduce(self, source_text: str, raw: str) -> CompiledRuleDraft:
        payload = self._parse_json(raw)
        self._reduce_refusal(payload)
        decision = self._reduce_decision(payload)
        selector = self._reduce_selector(payload)
        # F-30 presence semantics: omitted TTL takes the default; an explicit
        # null is not a number and rejects downstream.
        if "ttl_seconds" in payload:
            ttl = self._reduce_ttl(payload["ttl_seconds"])
        else:
            ttl = min(DEFAULT_RULE_TTL_S, self._max_ttl)

        rule_id = f"compiled-{uuid.uuid4().hex[:12]}"
        validate_rule_id(rule_id)
        return CompiledRuleDraft(
            rule_id=rule_id,
            decision=decision,
            selector=selector,
            ttl_seconds=ttl,
            source_text=source_text,
            description=describe_compiled_rule(
                decision, selector, ttl, registry=self._registry
            ),
            compiled_at=self._clock(),
        )

    def _parse_json(self, raw: str) -> dict[str, Any]:
        # One deterministic mechanical tolerance (F-33): a COMPLETELY
        # surrounding code fence — opening line AND closing marker — is
        # stripped before strict JSON. Bare JSON is accepted; a half-open
        # fence or any other wrapper is unparseable, not fuzzed into shape.
        text = raw.strip()
        if text.startswith("```"):
            if not text.endswith("```"):
                raise RuleCompileRejected(
                    "unparseable_response",
                    "unterminated code fence in the model output; "
                    "nothing was stored",
                )
            first_newline = text.find("\n")
            if first_newline == -1:
                raise RuleCompileRejected(
                    "unparseable_response",
                    "empty code fence in the model output; nothing was stored",
                )
            text = text[first_newline + 1 :].rstrip()
            if text.endswith("```"):
                text = text[:-3].rstrip()
        try:
            payload = _loads_strict(text)
        except (_StrictJsonError, json.JSONDecodeError) as exc:
            raise RuleCompileRejected(
                "unparseable_response",
                f"the model did not return strict JSON ({exc}); nothing was stored",
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

    def _reduce_refusal(self, payload: dict[str, Any]) -> None:
        """The model output is TAGGED, not merged (F-27): exactly one of

          refusal — expressible=false, explanation=<non-empty string>,
                    and NO rule fields
          rule    — expressible/explanation ABSENT, decision + selector
                    fields present

        Any mixed shape (an explanation floating through rule fields, an
        expressible flag beside a decision, a refusal without its
        explanation) rejects as invalid_shape — never interpreted."""
        tagged_refusal = any(k in payload for k in _REFUSAL_FIELD_KEYS)
        tagged_rule = any(k in payload for k in _RULE_FIELD_KEYS)
        if not tagged_refusal:
            return  # rule shape; decision is enforced downstream
        if tagged_rule:
            raise RuleCompileRejected(
                "invalid_shape",
                "mixed refusal/rule shape: refusal fields and rule fields in "
                "one object; nothing was stored",
            )
        expressible = payload.get("expressible")
        if not isinstance(expressible, bool) or expressible is not False:
            raise RuleCompileRejected(
                "invalid_shape",
                "a refusal must carry expressible=false; nothing was stored",
            )
        explanation = payload.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise RuleCompileRejected(
                "invalid_shape",
                "a refusal must carry a non-empty explanation string; "
                "nothing was stored",
            )
        raise RuleCompileRejected("inexpressible", explanation)

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
        selector = RuleSelector(**dims)
        self._check_satisfiable(selector)
        return selector

    def _string_dim(
        self, payload: dict[str, Any], name: str
    ) -> Optional[frozenset[str]]:
        # F-30: OMITTED means unspecified; PRESENT means it must satisfy the
        # declared type. JSON null is not a list — an explicit null must
        # reject, never widen the dimension to "anything".
        if name not in payload:
            return None
        value = payload[name]
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
        # F-30: presence semantics — explicit null rejects, only omission
        # leaves the dimension unspecified.
        if "risk_tiers" not in payload:
            return None
        value = payload["risk_tiers"]
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

    def _check_satisfiable(self, selector: RuleSelector) -> None:
        """F-22/F-28: a selector provably unable to match any registered
        action would be a silent no-op rule — reject it as inexpressible.

        Provable means: across the named actions (or every registered action
        when none is named), no candidate satisfies the tier and target
        constraints together — e.g. like with target 'tweet' (like targets
        posts), or like with tier private_reversible (like deterministically
        derives public_reversible_engagement). An action with EMPTY
        target_types has UNKNOWN target vocabulary: for a compiler whose
        rule is 'express exactly or reject', unknown can never ESTABLISH
        satisfiability — such a candidate proves nothing and the search
        continues; if no candidate with known, intersecting vocabulary
        remains, the selector rejects."""
        candidates = (
            sorted(selector.action_types)
            if selector.action_types is not None
            else self._registry.known_actions()
        )
        for action in candidates:
            meta, _ = self._registry.require(action)
            if (
                selector.risk_tiers is not None
                and meta.derive_tier() not in selector.risk_tiers
            ):
                continue
            if selector.target_types is not None:
                if not meta.target_types:
                    continue  # unknown vocabulary cannot prove satisfiability
                if not (set(meta.target_types) & set(selector.target_types)):
                    continue
            return  # one satisfiable candidate is enough
        raise RuleCompileRejected(
            "inexpressible",
            "the named action/tier/target conjunction matches no registered "
            "action — a rule that can never match would be a silent no-op; "
            "nothing was stored",
        )

    def _reduce_ttl(self, value: Any) -> float:
        # bools are ints in Python (the layer-1 parse lesson): reject them
        # rather than letting True parse as 1 second. An explicit JSON null
        # is not a number either — only omission takes the default (F-30).
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleCompileRejected(
                "invalid_shape",
                "ttl_seconds must be a number; nothing was stored",
            )
        ttl = float(value)
        if not math.isfinite(ttl):
            raise RuleCompileRejected(
                "inexpressible",
                "ttl must be a finite duration; nothing was stored",
            )
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

    def _revalidate_draft(self, draft: CompiledRuleDraft) -> None:
        """F-23: CompiledRuleDraft is public and constructible — confirm()
        must not trust it. Re-check every compiler constraint against the
        structural fields, and require the description to be exactly the
        canonical form of those fields."""
        if not isinstance(draft.decision, RuleDecision):
            raise RuleCompileError(
                "draft failed revalidation: decision is not a RuleDecision"
            )
        ttl = draft.ttl_seconds
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl <= 0
            or ttl > self._max_ttl
        ):
            raise RuleCompileError(
                "draft failed revalidation: ttl is outside this compiler's contract"
            )
        self._check_satisfiable(draft.selector)
        canonical = describe_compiled_rule(
            draft.decision, draft.selector, ttl, registry=self._registry
        )
        if draft.description != canonical:
            raise RuleCompileError(
                "draft failed revalidation: description is not the canonical "
                "compiled form of the structural fields"
            )

    def confirm(self, draft: CompiledRuleDraft, store: RuleStore) -> UserRule:
        """Persist one confirmed draft. The ONLY call that stores.

        The owner has confirmed ``draft.description`` — the canonical
        code-generated form, revalidated here because the draft dataclass is
        public and must not be trusted (F-23). The rule's TTL begins at
        CONFIRMATION time (F-20): ``compiled_at`` is draft/audit metadata
        and never owns the authority lifetime. Persistence goes through
        ``RuleStore.append_strict`` (F-19): a corrupt or unreadable store
        refuses mutation with bytes unchanged, duplicate ids refuse, and
        the read-modify-write is serialized against other same-path
        writers — a store failure can never widen what rules permit."""
        self._revalidate_draft(draft)
        rule = UserRule.create(
            selector=draft.selector,
            decision=draft.decision,
            rule_id=draft.rule_id,
            provenance="compiled",
            source_text=draft.source_text,
            ttl_seconds=draft.ttl_seconds,
            now=self._clock(),  # F-20: the rule's lifetime starts here
        )
        store.append_strict(rule)
        return rule
