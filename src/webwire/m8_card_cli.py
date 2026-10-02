"""M8 layer 4 — the Card CLI (frozen spec section 8 / build-order step 4).

The CLI scope is exactly what cards need — this is not a workflow
framework. Three commands:

    m8 card CAPABILITY PAYLOAD_JSON
        Build the LIVE runtime (dispatcher start → verified whoami), begin
        the write, render the card, and ask the owner NOW — the decision
        is only obtained after the preview is on screen (F-42: a decision
        supplied before the preview exists is preauthorization of an
        unseen snapshot). The runtime is stopped in a finally.

    m8 rules list
        Render every stored rule: the owner's original words, the canonical
        current interpretation, and remaining lifetime. Browser-free: this
        module imports the lifecycle surface directly and never touches the
        browser chain for rules commands (F-44).

    m8 rules reconfirm RULE_ID [--ttl SECONDS]
        Load and display the CURRENT rule — words, structure, new TTL —
        then ask the owner. Only a yes obtained after that display
        CAS-replaces the displayed snapshot; anything else mutates nothing.

Dependencies (runtime factory, decision reader, store, clock) are all
injectable, so the CLI is fully testable without a live session.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from webwire.config import WebWireConfig
from webwire.safety.m8_rule_lifecycle import (
    describe_rule,
    list_rules,
    reconfirm_rule,
)
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.user_rules import (
    DEFAULT_RULE_TTL_S,
    RuleStore,
    RuleStoreError,
)

__all__ = ["CardCli", "build_production_runtime", "main"]

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_USAGE = 2


def _terminal_decision(prompt: str) -> str:
    """The production decision reader: the owner answers at the terminal,
    after the snapshot is displayed."""
    try:
        return input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return ""


async def build_production_runtime(
    config: WebWireConfig,
    *,
    dispatcher_factory: Optional[Callable[..., Any]] = None,
    session_factory: Optional[Callable[[WebWireConfig], Any]] = None,
) -> Any:
    """Build the LIVE card runtime (F-41): a started dispatcher with a
    whoami-VERIFIED actor identity.

    The lifecycle is exactly the production requirement: start() (installs
    the M5 authority stack), then a whoami invoke whose success resolves
    the actor handle (migrated writes refuse without it). Any failure stops
    the dispatcher before the error leaves this function. The caller owns
    ``stop()`` afterwards.

    Both factories are injectable so the wiring itself is testable without
    a browser."""
    from webwire.dispatcher import Dispatcher
    from webwire.offline_recovery import canonical_config
    from webwire.session import SessionManager

    make_dispatcher = dispatcher_factory or Dispatcher
    make_session = session_factory or SessionManager
    # F-54 / RV11: canonicalize ONCE, BEFORE constructing either object, so
    # the session manager's persistence paths and the Dispatcher's safety
    # state share the exact same absolute authority domain even if the
    # caller supplied a relative state_dir and the CWD later changes.
    config = canonical_config(config)
    session = make_session(config)
    dispatcher = make_dispatcher(config, session_manager=session)
    try:
        started = await dispatcher.start()
        if not started.ok:
            raise RuntimeError(f"dispatcher start failed: {getattr(started.error, 'message', 'unknown error')}")
        whoami = await dispatcher.invoke("whoami", {})
        if not whoami.ok:
            raise RuntimeError(
                "whoami failed — no verified actor identity for card writes: "
                f"{getattr(whoami.error, 'message', 'unknown error')}"
            )
        # F-51: verify the state the write path actually trusts, not merely
        # response data. set_resolved_handle() strips and refuses blank
        # values, so a whitespace-only response handle leaves resolved_handle
        # unset — and migrated writes refuse without it.
        actor = getattr(session, "resolved_handle", None)
        if not actor:
            raise RuntimeError(
                "whoami did not establish a resolved actor identity — card writes would be refused"
            )
    except BaseException:
        try:
            await dispatcher.stop()
        except Exception:  # noqa: BLE001 — best-effort cleanup on failure
            pass
        raise
    return dispatcher


class CardCli:
    """The command controller; every dependency is injectable for tests."""

    def __init__(
        self,
        *,
        store: RuleStore,
        runtime_factory: Optional[Callable[[], Awaitable[Any]]] = None,
        decision_reader: Callable[[str], str] = _terminal_decision,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._runtime_factory = runtime_factory
        self._decision_reader = decision_reader
        self._clock = clock

    def _ask(self, prompt: str) -> bool:
        answer = self._decision_reader(prompt)
        return answer in ("y", "yes")

    # -- m8 card ---------------------------------------------------------

    async def cmd_card(self, capability: str, payload_json: str) -> int:
        if self._runtime_factory is None:
            print(
                "card: no live runtime configured (rules commands work without one)",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            print(f"card: payload is not valid JSON ({exc})", file=sys.stderr)
            return _EXIT_USAGE
        if not isinstance(payload, dict):
            print("card: payload must be a JSON object", file=sys.stderr)
            return _EXIT_USAGE
        if "confirmation_token" in payload:
            # F-54: reserved confirmation authority is rejected BEFORE any
            # live-session work — no runtime is built, no browser starts,
            # no whoami runs. CardFlow enforces the same rule at its own
            # boundary (F-48); this is the CLI's controlled usage error.
            print(
                "card: payload carries a reserved confirmation_token — "
                "confirmation authority may only be minted by phase 1 and "
                "held inside the card",
                file=sys.stderr,
            )
            return _EXIT_USAGE

        # Imported lazily: the card surface needs the browser result types,
        # and rules-only invocations of this CLI must not (F-44).
        from webwire.m8_cards import CardFlow

        try:
            dispatcher = await self._runtime_factory()
        except RuntimeError as exc:
            # M7 Layer 2: a transient authority process (this CLI) loses the
            # ownership race against a live runtime — surface the controlled
            # authority_busy/start failure instead of a traceback.
            print(f"card: {exc}", file=sys.stderr)
            return _EXIT_ERROR
        try:
            flow = CardFlow(dispatcher.invoke)
            result, card = await flow.begin(capability, payload)
            if card is None:
                policy: dict[str, Any] = {}
                if isinstance(result.data, dict) and isinstance(result.data.get("policy"), dict):
                    policy = result.data["policy"]
                if policy.get("verdict") == "confirmation_required":
                    # F-52: a confirmation-required response with no usable
                    # card is a PROTOCOL failure — neither execution nor
                    # usable approval authority exists. Fail closed with a
                    # non-zero exit, never a success.
                    print(
                        "card error: the kernel asked for confirmation but "
                        "the response carried no usable approval carrier — "
                        "nothing was executed and nothing can be approved",
                        file=sys.stderr,
                    )
                    return _EXIT_ERROR
                # The kernel decided without a human (rule-ALLOW or NEVER):
                # report the outcome, there is nothing to ask.
                print(f"no card: verdict={policy.get('verdict')!r} blocked_by={policy.get('blocked_by')!r}")
                return _EXIT_OK if result.ok else _EXIT_ERROR

            # F-42: render FIRST, obtain the decision NOW. The decision
            # binds to exactly what is on screen above this prompt.
            print(card.render_text())
            if self._ask(f"approve {capability}?"):
                final = await card.approve()
            else:
                card.deny()
                print("denied — nothing executed")
                return _EXIT_OK
            final_policy: dict[str, Any] = {}
            trace: dict[str, Any] = {}
            if isinstance(final.data, dict):
                if isinstance(final.data.get("policy"), dict):
                    final_policy = final.data["policy"]
                if isinstance(final.data.get("trace"), dict):
                    trace = final.data["trace"]
            print(f"approved: verdict={final_policy.get('verdict')!r} execute_ok={trace.get('execute_ok')}")
            return _EXIT_OK if final.ok else _EXIT_ERROR
        finally:
            await dispatcher.stop()

    # -- m8 rules list ---------------------------------------------------

    def cmd_rules_list(self) -> int:
        cards = list_rules(self._store, now=self._clock(), registry=DEFAULT_REGISTRY)
        if not cards:
            print("no rules stored (a missing or corrupt store lists empty)")
            return _EXIT_OK
        for card in cards:
            print(card.render_text())
            print()
        return _EXIT_OK

    # -- m8 rules reconfirm ----------------------------------------------

    def cmd_rules_reconfirm(self, rule_id: str, ttl_seconds: float) -> int:
        # Load and display the CURRENT rule; the owner decides on exactly
        # what is displayed (F-42: the human decision is chronological —
        # it cannot predate the snapshot on screen).
        expected = next((r for r in self._store.load() if r.rule_id == rule_id), None)
        if expected is None:
            print(f"reconfirm: rule_id {rule_id!r} not found — nothing to re-confirm", file=sys.stderr)
            return _EXIT_ERROR
        print(f"re-confirming {rule_id!r}:")
        print(f"  {describe_rule(expected)}")
        if expected.source_text:
            print(f'  owner\'s words: "{expected.source_text}"')
        print(f"  new TTL: {ttl_seconds:.0f}s (max {DEFAULT_RULE_TTL_S:.0f}s)")

        if not self._ask("re-confirm this exact rule?"):
            print("declined — nothing changed")
            return _EXIT_OK
        try:
            reconfirmed = reconfirm_rule(
                self._store,
                expected,
                ttl_seconds=ttl_seconds,
                now=self._clock(),
            )
        except ValueError as exc:
            print(f"reconfirm: {exc}", file=sys.stderr)
            return _EXIT_USAGE
        except RuleStoreError as exc:
            print(f"reconfirm: {exc}", file=sys.stderr)
            return _EXIT_ERROR
        print(f"re-confirmed: expires_at={reconfirmed.expires_at:.0f}")
        return _EXIT_OK

    # -- dispatch ----------------------------------------------------------

    async def run(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser(
            prog="m8",
            description="Card CLI + rule lifecycle (frozen M8 spec section 8)",
        )
        sub = parser.add_subparsers(dest="command", required=True)

        p_card = sub.add_parser("card", help="begin a card-mediated write through the live runtime")
        p_card.add_argument("capability")
        p_card.add_argument("payload", help="JSON object payload")

        p_list = sub.add_parser("rules", help="rule lifecycle (browser-free)")
        rules_sub = p_list.add_subparsers(dest="rules_command", required=True)
        rules_sub.add_parser("list", help="list stored rules")

        p_re = rules_sub.add_parser("reconfirm", help="re-confirm a rule's TTL")
        p_re.add_argument("rule_id")
        p_re.add_argument("--ttl", type=float, default=DEFAULT_RULE_TTL_S)

        args = parser.parse_args(argv)
        if args.command == "card":
            return await self.cmd_card(args.capability, args.payload)
        if args.rules_command == "list":
            return self.cmd_rules_list()
        return self.cmd_rules_reconfirm(args.rule_id, args.ttl)


def main(argv: Optional[list[str]] = None) -> int:
    """Console entry point: builds live wiring from configuration. The
    runtime factory is only ever invoked by the card command — rules
    commands never touch the browser chain."""

    async def _run() -> int:
        config = WebWireConfig()
        cli = CardCli(
            store=RuleStore(config.rules_path()),
            runtime_factory=lambda: build_production_runtime(config),
        )
        return await cli.run(sys.argv[1:] if argv is None else argv)

    import asyncio

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
