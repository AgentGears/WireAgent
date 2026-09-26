"""Narrow local M6 operator workflow for terminal reconciliation.

This is intentionally not a Dispatcher capability and exposes no browser
mutation surface. It stages a read-only target plus operator-supplied evidence,
displays an exact verdict/evidence binding, requires explicit same-session text
confirmation, then asks the coordinator to mint one short-lived
ReconciliationAuthority bound to that coordinator's protocol domain.

Browser-backed evidence collection remains an action-specific read-only adapter;
this workflow never turns a failed observation into CONFIRMED_NO_EFFECT.

Source of truth: ``docs/M6_DESIGN.md`` §§9-10, 12, 16.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from webwire.safety.reconciliation_authority import (
    DEFAULT_RECONCILIATION_AUTHORITY_TTL_S,
    ReconciliationAuthority,
)
from webwire.safety.reconciliation_coordinator import (
    ReconciliationCoordinator,
    ReconciliationDenied,
    ReconciliationPublicationError,
    ReconciliationResolution,
    ReconciliationTarget,
)
from webwire.safety.reconciliation_ledger import (
    ReconciliationVerdict,
    canonical_evidence_hash,
)

__all__ = [
    "ReconciliationOperatorError",
    "ReconciliationOperatorSession",
    "ReconciliationProposal",
]


class ReconciliationOperatorError(RuntimeError):
    """The local operator workflow was used inconsistently or without confirmation."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"reconciliation operator workflow denied: {reason}"
            + (f" — {detail}" if detail else "")
        )


@dataclass(frozen=True)
class ReconciliationProposal:
    """Immutable display binding for one proposed terminal reconciliation."""

    proposal_id: str
    effect_id: str
    semantic_key: str
    action_type: str
    intent_hash: str
    policy_binding: str
    actor_id: Optional[str]
    target_type: Optional[str]
    target_id: Optional[str]
    verdict: ReconciliationVerdict
    operator_id: str
    evidence_hash: str
    evidence_summary: str
    confirmation_text: str
    _evidence_json: str

    @property
    def evidence(self) -> dict[str, Any]:
        decoded = json.loads(self._evidence_json)
        if not isinstance(decoded, dict):  # construction guarantees strict object
            raise AssertionError("proposal evidence is not an object")
        return decoded


@dataclass
class _ProposalState:
    proposal: ReconciliationProposal
    authority: Optional[ReconciliationAuthority] = None
    resolved: bool = False


class ReconciliationOperatorSession:
    """One local interactive operator session.

    Confirmation is intentionally exact and session-scoped. Repeating the exact
    confirmation returns the same authority object rather than minting parallel
    authorities for one human approval. Persistence failure therefore retries
    through the coordinator with the same committed authority/frozen fact.
    """

    def __init__(
        self,
        *,
        coordinator: ReconciliationCoordinator,
        operator_id: str,
        authority_ttl_seconds: float = DEFAULT_RECONCILIATION_AUTHORITY_TTL_S,
        monotonic_clock: Callable[[], float] = time.monotonic,
        proposal_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(12),
    ) -> None:
        if not isinstance(coordinator, ReconciliationCoordinator):
            raise TypeError("coordinator must be a ReconciliationCoordinator")
        if not isinstance(operator_id, str) or not operator_id:
            raise ValueError("operator_id must be a non-empty string")
        self._coordinator = coordinator
        self._operator_id = operator_id
        self._authority_ttl_seconds = authority_ttl_seconds
        self._monotonic_clock = monotonic_clock
        self._proposal_id_factory = proposal_id_factory
        self._lock = threading.RLock()
        self._proposals: dict[str, _ProposalState] = {}

    @property
    def operator_id(self) -> str:
        return self._operator_id

    def list_targets(self) -> tuple[ReconciliationTarget, ...]:
        """Read-only equivalent of ``webwire recovery list``."""
        return self._coordinator.list_targets()

    def show_target(self, effect_id: str) -> ReconciliationTarget:
        """Read-only equivalent of ``webwire recovery show <effect_id>``."""
        return self._coordinator.describe_target(effect_id)

    def prepare_resolution(
        self,
        *,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence: dict[str, Any],
        evidence_summary: str,
    ) -> ReconciliationProposal:
        """Freeze one displayed proposal without minting terminal authority."""
        if not isinstance(verdict, ReconciliationVerdict):
            raise ReconciliationOperatorError("invalid_verdict")
        if not isinstance(evidence_summary, str) or not evidence_summary.strip():
            raise ReconciliationOperatorError("evidence_summary_missing")

        target = self._coordinator.describe_target(effect_id)
        try:
            # First validate the caller-owned object without coercion, then
            # serialize the exact snapshot that will be retained. Re-hashing the
            # detached copy ensures the displayed authority binding and retained
            # evidence are one fact even if caller mutation races preparation.
            initial_hash = canonical_evidence_hash(evidence)
            evidence_json = json.dumps(
                evidence,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            frozen_evidence = json.loads(evidence_json)
            if not isinstance(frozen_evidence, dict):
                raise ValueError("evidence must serialize to a JSON object")
            evidence_hash = canonical_evidence_hash(frozen_evidence)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ReconciliationOperatorError("invalid_evidence", str(exc)) from exc
        if evidence_hash != initial_hash:
            raise ReconciliationOperatorError(
                "evidence_changed_during_prepare",
                "caller evidence changed while the proposal was being frozen",
            )

        proposal_id = self._proposal_id_factory()
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ReconciliationOperatorError("proposal_id_invalid")
        confirmation_text = f"CONFIRM {effect_id} {verdict.value} {evidence_hash}"
        first = target.first_record
        proposal = ReconciliationProposal(
            proposal_id=proposal_id,
            effect_id=effect_id,
            semantic_key=first.semantic_key,
            action_type=first.action_type,
            intent_hash=first.intent_hash,
            policy_binding=first.policy_binding,
            actor_id=first.actor_id,
            target_type=first.target_type,
            target_id=first.target_id,
            verdict=verdict,
            operator_id=self._operator_id,
            evidence_hash=evidence_hash,
            evidence_summary=evidence_summary.strip(),
            confirmation_text=confirmation_text,
            _evidence_json=evidence_json,
        )
        with self._lock:
            if proposal_id in self._proposals:
                raise ReconciliationOperatorError("proposal_id_collision")
            self._proposals[proposal_id] = _ProposalState(proposal=proposal)
        return proposal

    def confirm_resolution(
        self,
        proposal_id: str,
        *,
        confirmation_text: str,
    ) -> ReconciliationAuthority:
        """Mint terminal authority only after exact explicit same-session confirmation."""
        with self._lock:
            state = self._proposals.get(proposal_id)
            if state is None:
                raise ReconciliationOperatorError("proposal_unknown")
            if state.resolved:
                raise ReconciliationOperatorError("proposal_resolved")
            proposal = state.proposal
            if confirmation_text != proposal.confirmation_text:
                raise ReconciliationOperatorError("confirmation_mismatch")
            if state.authority is None:
                state.authority = self._coordinator._mint_operator_authority(
                    effect_id=proposal.effect_id,
                    verdict=proposal.verdict,
                    evidence_hash=proposal.evidence_hash,
                    operator_id=proposal.operator_id,
                    ttl_seconds=self._authority_ttl_seconds,
                    monotonic_clock=self._monotonic_clock,
                )
            return state.authority

    def resolve(
        self,
        proposal_id: str,
        *,
        authority: ReconciliationAuthority,
    ) -> ReconciliationResolution:
        """Persist the exact confirmed proposal through the coordinator."""
        with self._lock:
            state = self._proposals.get(proposal_id)
            if state is None:
                raise ReconciliationOperatorError("proposal_unknown")
            if state.resolved:
                raise ReconciliationOperatorError("proposal_resolved")
            if state.authority is None:
                raise ReconciliationOperatorError("proposal_not_confirmed")
            if state.authority is not authority:
                raise ReconciliationOperatorError("authority_mismatch")
            proposal = state.proposal

        try:
            resolution = self._coordinator.resolve(
                proposal.effect_id,
                proposal.verdict,
                proposal.evidence,
                authority,
            )
        except ReconciliationPublicationError:
            # Durable success already consumed authority. The workflow must not
            # offer a second resolution attempt; guard refresh can be retried by
            # normal recovery hydration/refresh without new terminal authority.
            with self._lock:
                state.resolved = True
            raise
        except ReconciliationDenied:
            raise
        else:
            with self._lock:
                state.resolved = True
            return resolution
