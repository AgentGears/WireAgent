"""M8 layer 4 — the Card CLI (frozen spec section 8 / build-order step 4).

The CLI scope is exactly what cards need — this is not a workflow
framework. Three commands:

    m8 card CAPABILITY PAYLOAD_JSON [--yes | --deny]
        Begin a write through the card surface, render the card, and — with
        an explicit decision flag — carry it through phase 2 in the same
        run. Without a flag the card is shown and the pending token dies
        with the process (the card surface never reveals it).

    m8 rules list
        Render every stored rule: the owner's original words, the canonical
        current interpretation (ceiling-aware), and remaining lifetime.

    m8 rules reconfirm RULE_ID [--ttl SECONDS] [--yes]
        Re-confirm one rule's TTL: display exactly what will be re-issued,
        then — only with --yes — replace the reviewed snapshot under the
        store's compare-and-swap fence. A rule that changed since the
        display conflicts with zero mutation.

The controller takes injected dependencies (an invoke callable and a rule
store), so it is fully testable without a live session; ``main`` builds the
live wiring from configuration. The browser session is only required by the
``card`` command in production and is constructed lazily on first use.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from webwire.config import WebWireConfig
from webwire.m8_cards import (
    CardFlow,
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

__all__ = ["CardCli", "main"]

_Invoke = Callable[[str, dict[str, Any]], Awaitable[Any]]
_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_USAGE = 2


class CardCli:
    """The command controller; every dependency is injectable for tests."""

    def __init__(
        self,
        *,
        store: RuleStore,
        invoke: Optional[_Invoke] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._invoke = invoke
        self._clock = clock

    # -- m8 card ---------------------------------------------------------

    async def cmd_card(self, capability: str, payload_json: str, decision: Optional[str]) -> int:
        if self._invoke is None:
            print(
                "card: no invoke route configured (a live session is required for the card command)",
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

        flow = CardFlow(self._invoke)
        result, card = await flow.begin(capability, payload)
        if card is None:
            # The kernel decided without a human (rule-ALLOW or NEVER):
            # report the outcome, there is nothing to approve.
            policy = {}
            if isinstance(result.data, dict) and isinstance(result.data.get("policy"), dict):
                policy = result.data["policy"]
            print(f"no card: verdict={policy.get('verdict')!r} blocked_by={policy.get('blocked_by')!r}")
            return _EXIT_OK if result.ok else _EXIT_ERROR

        print(card.render_text())
        if decision is None:
            print("pending: no decision flag given; nothing was executed and the token dies with this process")
            return _EXIT_OK
        if decision == "deny":
            card.deny()
            print("denied — nothing executed")
            return _EXIT_OK
        final = await card.approve()
        final_policy: dict[str, Any] = {}
        trace: dict[str, Any] = {}
        if isinstance(final.data, dict):
            if isinstance(final.data.get("policy"), dict):
                final_policy = final.data["policy"]
            if isinstance(final.data.get("trace"), dict):
                trace = final.data["trace"]
        print(
            f"approved: verdict={final_policy.get('verdict')!r} "
            f"execute_ok={trace.get('execute_ok')}"
        )
        return _EXIT_OK if final.ok else _EXIT_ERROR

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

    def cmd_rules_reconfirm(self, rule_id: str, ttl_seconds: float, confirmed: bool) -> int:
        # Read and display in the same breath: the snapshot shown here is
        # the exact snapshot the compare-and-swap replace is bound to.
        expected = next((r for r in self._store.load() if r.rule_id == rule_id), None)
        if expected is None:
            print(f"reconfirm: rule_id {rule_id!r} not found — nothing to re-confirm", file=sys.stderr)
            return _EXIT_ERROR
        print(f"re-confirming {rule_id!r}:")
        print(f"  {describe_rule(expected)}")
        if expected.source_text:
            print(f'  owner\'s words: "{expected.source_text}"')
        print(f"  new TTL: {ttl_seconds:.0f}s (max {DEFAULT_RULE_TTL_S:.0f}s)")
        if not confirmed:
            print("dry run — pass --yes to re-confirm")
            return _EXIT_OK
        try:
            reconfirmed = reconfirm_rule(
                self._store,
                expected,
                ttl_seconds=ttl_seconds,
                now=self._clock(),
            )
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

        p_card = sub.add_parser("card", help="begin a card-mediated write")
        p_card.add_argument("capability")
        p_card.add_argument("payload", help="JSON object payload")
        group = p_card.add_mutually_exclusive_group()
        group.add_argument(
            "--yes", action="store_const", const="approve", dest="decision", help="approve the card now"
        )
        group.add_argument(
            "--deny", action="store_const", const="deny", dest="decision", help="deny the card now"
        )

        p_list = sub.add_parser("rules", help="rule lifecycle")
        rules_sub = p_list.add_subparsers(dest="rules_command", required=True)
        rules_sub.add_parser("list", help="list stored rules")

        p_re = rules_sub.add_parser("reconfirm", help="re-confirm a rule's TTL")
        p_re.add_argument("rule_id")
        p_re.add_argument("--ttl", type=float, default=DEFAULT_RULE_TTL_S)
        p_re.add_argument("--yes", action="store_true", help="confirm after displaying the rule")

        args = parser.parse_args(argv)
        if args.command == "card":
            return await self.cmd_card(args.capability, args.payload, args.decision)
        if args.rules_command == "list":
            return self.cmd_rules_list()
        return self.cmd_rules_reconfirm(args.rule_id, args.ttl, args.yes)


def main(argv: Optional[list[str]] = None) -> int:
    """Console entry point: builds live wiring from configuration."""

    async def _run() -> int:
        config = WebWireConfig()
        store = RuleStore(config.rules_path())
        invoke: Optional[_Invoke] = None
        try:
            from webwire.dispatcher import Dispatcher
            from webwire.session import SessionManager

            session = SessionManager(config)
            dispatcher = Dispatcher(config, session_manager=session)
            invoke = dispatcher.invoke
        except Exception as exc:  # noqa: BLE001 — degrade to rules-only CLI
            print(f"m8: live session unavailable ({exc}); rules commands still work", file=sys.stderr)
        cli = CardCli(store=store, invoke=invoke)
        return await cli.run(sys.argv[1:] if argv is None else argv)

    import asyncio

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
