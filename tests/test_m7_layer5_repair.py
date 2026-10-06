"""M7 Layer 5 first-review repairs — F-72 (safety projection), F-73
(reconciliation wire route), F-74 (task ownership): pipeline-level
regressions (platform-portable; the real-socket equivalents live in
test_m7_layer5_repair_transport.py).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from super_browser.results import ActionError, ErrorCategory, action_result

from webwire.authority_ipc_framing import encode_json_frame, parse_frame_header
from webwire.authority_ipc_protocol import IPC_PROTOCOL_VERSION, compute_runtime_build_id
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()


def _uncertain_effect_failure() -> Any:
    """A REAL M5-style uncertain-effect failure: ok=False, an external
    effect MAY exist, reconciliation IS required."""
    result = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.UNKNOWN,
            message="submit clicked but posted URL not captured; public content MAY exist",
            recoverable=False,
        ),
    )
    result.data = {
        "policy": {"verdict": "allow", "blocked_by": None, "risk_tier": "public_content_irreversible"},
        "trace": {"stages": ["intent_created", "confirmation_required", "submit_clicked_verification_pending"]},
        "public_side_effect": True,
        "reconciliation_required": True,
        "m5_effect_state": "effect_unknown",
        "semantic_key": "actor|post|none|abc123",
        "data": {"failure_code": "submit_clicked_verification_pending", "posted_url": None},
    }
    return result


def _clean_denial() -> Any:
    """A clean policy denial: proven no-effect."""
    result = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.SECURITY,
            message="denied by standing user rule",
            recoverable=False,
        ),
    )
    result.data = {
        "policy": {"verdict": "deny", "blocked_by": "user_rule", "risk_tier": "private_reversible"},
        "trace": {"stages": ["intent_created", "denied:user_rule"]},
        "public_side_effect": False,
        "reconciliation_required": False,
        "data": {"rule_id": "rule-7", "decision": "never"},
    }
    return result


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("layer5-repair"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _server(session: AuthoritySession, invoke: Any, *, provider: Any = None) -> AuthorityIPCServer:
    return AuthorityIPCServer(
        session=session,
        authority_domain=Path("layer5-repair"),
        invoke=invoke,
        runtime_build_id=BUILD_ID,
        reconciliation_provider=provider,
    )


def _frame(operation: str, payload: Any, *, instance_id: str, request_id: str | None = None) -> bytes:
    body: dict[str, Any] = {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "request_id": request_id or secrets.token_hex(16),
        "authority_instance_id": instance_id,
        "runtime_build_id": BUILD_ID,
    }
    return encode_json_frame(body)


async def _respond(server: AuthorityIPCServer, frame: bytes) -> dict[str, Any]:
    header, consumed = parse_frame_header(frame)
    return json.loads((await server.process_request(header, frame[consumed:]))[8:])


# ---------------------------------------------------------------------------
# F-72: mutation failure safety state survives the boundary
# ---------------------------------------------------------------------------


async def test_uncertain_effect_failure_preserves_safety_state() -> None:
    """F-72: ok=False with public_side_effect=True, reconciliation_required
    =True, m5_effect_state=effect_unknown → the WIRE response preserves
    all three facts (plus policy verdict and trace stages) — not a bare
    capability error."""
    session = _session_ready()
    calls: list = []

    async def invoke(name: str, payload: dict) -> Any:
        calls.append(name)
        return _uncertain_effect_failure()

    server = _server(session, invoke)
    rid = secrets.token_hex(16)
    frame = _frame("post_text", {"text": "x"}, instance_id=session.authority_instance_id, request_id=rid)

    response = await _respond(server, frame)
    assert response["ok"] is False
    assert response["error"]["code"] == "capability"
    safety = response["safety"]
    assert safety["public_side_effect"] is True
    assert safety["reconciliation_required"] is True
    assert safety["m5_effect_state"] == "effect_unknown"
    assert safety["semantic_key"] == "actor|post|none|abc123"
    assert safety["policy"]["blocked_by"] is None
    assert "submit_clicked_verification_pending" in safety["trace_stages"]
    assert safety["data"]["failure_code"] == "submit_clicked_verification_pending"


async def test_uncertain_effect_retained_retry_identical_no_reexecution() -> None:
    """F-72 + §10.5: the retained retry of the SAME failed mutation
    returns the IDENTICAL frame (safety state included) with no second
    execution."""
    session = _session_ready()
    calls: list = []

    async def invoke(name: str, payload: dict) -> Any:
        calls.append(name)
        return _uncertain_effect_failure()

    server = _server(session, invoke)
    rid = secrets.token_hex(16)
    frame = _frame("post_text", {"text": "x"}, instance_id=session.authority_instance_id, request_id=rid)

    first = await _respond(server, frame)
    second = await _respond(server, frame)
    assert second == first
    assert second["safety"]["reconciliation_required"] is True
    assert len(calls) == 1


async def test_clean_denial_clearly_distinguished_from_uncertain_effect() -> None:
    """F-72: a proven-no-effect denial is distinguishable on the wire from
    an uncertain external mutation — no side-effect markers, explicit
    blocked_by."""
    session = _session_ready()

    async def invoke(name: str, payload: dict) -> Any:
        return _clean_denial()

    server = _server(session, invoke)
    response = await _respond(
        server, _frame("bookmark_post", {"post_url": "u"}, instance_id=session.authority_instance_id)
    )
    assert response["ok"] is False
    safety = response["safety"]
    assert safety["public_side_effect"] is False
    assert safety["reconciliation_required"] is False
    assert safety["policy"]["blocked_by"] == "user_rule"

    # The distinguishing invariant, explicitly: uncertainty carries the
    # side-effect markers; the clean denial does not.
    uncertain = await _respond(
        _server(session, _result_invoke(_uncertain_effect_failure())),
        _frame("post_text", {"text": "x"}, instance_id=session.authority_instance_id),
    )
    assert uncertain["safety"]["public_side_effect"] is True
    assert uncertain["safety"]["reconciliation_required"] is True


def _result_invoke(result: Any):
    """An invoke double returning one fixed ActionResult."""

    async def _invoke(name: str, payload: dict) -> Any:
        return result

    return _invoke


# ---------------------------------------------------------------------------
# F-74: the table owns the admitted task
# ---------------------------------------------------------------------------


async def test_new_request_pins_owner_task_in_table() -> None:
    """F-74 (§10.5): while a NEW request executes, its in-flight entry
    holds the strong owner-side TASK reference — not just a future."""
    session = _session_ready()
    release = asyncio.Event()

    async def invoke(name: str, payload: dict) -> Any:
        await release.wait()
        return ok_result(data={"done": True})

    server = _server(session, invoke)
    rid = secrets.token_hex(16)
    frame = _frame("post_text", {"text": "x"}, instance_id=session.authority_instance_id, request_id=rid)

    task = asyncio.create_task(_respond(server, frame))
    await asyncio.sleep(0.05)
    entry = server._table._inflight[rid]
    assert entry.task is not None and not entry.task.done(), "the task is pinned, live"
    release.set()
    response = await task
    assert response["ok"] is True
    assert rid not in server._table._inflight


# ---------------------------------------------------------------------------
# F-73: the reconciliation operator wire route (§14.3)
# ---------------------------------------------------------------------------


class _FakeOperatorSession:
    """Duck-typed stand-in mirroring OwnedReconciliationOperatorSession
    (the real one is exercised over a REAL Dispatcher in the transport
    repair suite)."""

    def __init__(self, operator_id: str) -> None:
        self.operator_id = operator_id
        self.calls: list = []

    def list_targets(self) -> Any:
        self.calls.append("list")

        class _Rec:
            semantic_key = "actor|like|post|t1"
            action_type = "like"
            intent_hash = "h1"
            policy_binding = "policy-t1"
            actor_id = "actor"
            target_type = "post"
            target_id = "t1"

        first = _Rec()
        first.timestamp = "2026-10-06T00:00:00+00:00"
        first.state = type("S", (), {"value": "reserved"})()
        last = _Rec()
        last.timestamp = "2026-10-06T01:00:00+00:00"
        last.state = type("S", (), {"value": "effect_unknown"})()

        class _Target:
            effect_id = "fx-1"
            first_record = first
            last_record = last

        return (_Target(),)

    def show_target(self, effect_id: str) -> Any:
        self.calls.append(("show", effect_id))
        return self.list_targets()[0]

    def list_targets_page(self, *, limit: int, after_effect_id=None) -> Any:
        targets = self.list_targets()
        ids = sorted(t.effect_id for t in targets)
        eligible = [i for i in ids if after_effect_id is None or i > after_effect_id]
        page_ids = eligible[:limit]
        by_id = {t.effect_id: t for t in targets}
        return (
            [by_id[i] for i in page_ids],
            len(ids),
            len(eligible),
            len(eligible) > len(page_ids),
        )

    def prepare_resolution(self, *, effect_id: str, verdict: Any, evidence: dict, evidence_summary: str) -> Any:
        self.calls.append(("prepare", effect_id))
        operator_id = self.operator_id

        class _Proposal:
            proposal_id = "prop-1"
            semantic_key = "actor|like|post|t1"
            action_type = "like"
            intent_hash = "h1"
            policy_binding = "policy-t1"
            actor_id = "actor"
            target_type = "post"
            target_id = "t1"
            evidence_hash = "eh-1"
            confirmation_text_suffix = f"eh-1 {operator_id}"

        proposal = _Proposal()
        proposal.effect_id = effect_id  # class bodies cannot close over parameters
        proposal.verdict = verdict
        proposal.operator_id = operator_id
        proposal.evidence_summary = evidence_summary
        proposal.confirmation_text = f"CONFIRM {effect_id} {verdict.value} eh-1 {operator_id}"
        return proposal

        return _Proposal()

    def confirm_resolution(self, proposal_id: str, *, confirmation_text: str) -> Any:
        self.calls.append(("confirm", proposal_id))
        assert confirmation_text.endswith(self.operator_id)
        return object()  # the authority: must NEVER cross the wire

    def resolve(self, proposal_id: str, *, authority: Any) -> Any:
        self.calls.append(("resolve", proposal_id))

        class _Resolution:
            confirmation_epoch = 1

            class record:
                reconciliation_id = "rc-1"
                effect_id = "fx-1"

            class recovery_status:
                hydrated = True
                available = True
                unresolved_semantic_keys = ()
                unresolved_effect_count = 0

        return _Resolution()


async def _recon(server: AuthorityIPCServer, instance: str, operation: str, payload: dict) -> dict:
    return await _respond(server, _frame(operation, payload, instance_id=instance))


async def test_reconciliation_wire_round_trip_authority_stays_private() -> None:
    """F-73: open → list → show → prepare → confirm → resolve over the
    pipeline. The minted authority object NEVER crosses the wire; the
    client confirms by exact text and addresses the proposal by id."""
    session = _session_ready()
    opened: list = []

    def provider(operator_id: str) -> Any:
        opened.append(operator_id)
        return _FakeOperatorSession(operator_id)

    server = _server(session, invoke=_noop_invoke, provider=provider)
    instance = session.authority_instance_id

    r = await _recon(server, instance, "reconciliation_open", {"operator_id": "local-admin"})
    assert r["ok"] is True, r
    wire_id = r["data"]["reconciliation_session_id"]

    r = await _recon(server, instance, "reconciliation_list", {"reconciliation_session_id": wire_id})
    assert r["ok"] is True
    target = r["data"]["targets"][0]
    assert target["effect_id"] == "fx-1"
    # F-76: state is the CURRENT (last) record; first/current explicit;
    # the full lineage (incl. policy_binding) is present.
    assert target["state"] == "effect_unknown"
    assert target["first_state"] == "reserved"
    assert target["current_state"] == "effect_unknown"
    assert target["current_timestamp"] == "2026-10-06T01:00:00+00:00"
    assert target["policy_binding"] == "policy-t1"

    r = await _recon(
        server,
        instance,
        "reconciliation_show",
        {"reconciliation_session_id": wire_id, "effect_id": "fx-1"},
    )
    assert r["ok"] is True
    assert r["data"]["target"]["semantic_key"] == "actor|like|post|t1"

    r = await _recon(
        server,
        instance,
        "reconciliation_prepare",
        {
            "reconciliation_session_id": wire_id,
            "effect_id": "fx-1",
            "verdict": "CONFIRMED_EFFECT",
            "evidence": {
                "basis": "operator-review",
                "observations": [{"kind": "operator", "value": "verified"}],
            },
            "evidence_summary": "Operator verified the terminal effect evidence.",
        },
    )
    assert r["ok"] is True, r
    proposal = r["data"]["proposal"]
    assert proposal["proposal_id"] == "prop-1"
    assert proposal["confirmation_text"].startswith("CONFIRM fx-1")
    assert proposal["policy_binding"] == "policy-t1"

    r = await _recon(
        server,
        instance,
        "reconciliation_confirm",
        {
            "reconciliation_session_id": wire_id,
            "proposal_id": "prop-1",
            "confirmation_text": proposal["confirmation_text"],
        },
    )
    assert r["ok"] is True, r
    # The confirm response contains NO authority object — ids only.
    assert set(r["data"].keys()) == {"confirmed", "proposal_id"}

    r = await _recon(
        server,
        instance,
        "reconciliation_resolve",
        {"reconciliation_session_id": wire_id, "proposal_id": "prop-1"},
    )
    assert r["ok"] is True, r
    assert r["data"]["resolution"]["confirmation_epoch"] == 1
    assert opened == ["local-admin"]


def _noop_invoke(name: str, payload: dict) -> Any:
    async def _invoke() -> Any:
        return ok_result(data={})

    return _invoke()


async def test_reconciliation_wrong_confirmation_text_rejected() -> None:
    """The M6 exact-text law: a confirm with anything but the exact frozen
    text fails without minting authority."""
    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    instance = session.authority_instance_id
    wire_id = (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
        "reconciliation_session_id"
    ]
    r = await _recon(
        server,
        instance,
        "reconciliation_confirm",
        {"reconciliation_session_id": wire_id, "proposal_id": "prop-9", "confirmation_text": "CONFIRM WRONG"},
    )
    assert r["ok"] is False
    assert r["error"]["code"] == "reconciliation"


async def test_reconciliation_sessions_revoked_at_drain_and_process_local() -> None:
    """§14.3: operator sessions are closed by drain; a successor owner
    (new server instance) never recognizes the old session id."""
    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    instance = session.authority_instance_id
    wire_id = (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
        "reconciliation_session_id"
    ]

    # Drain closes every logical operator session (§14.3) — and drain
    # also refuses new work outright, so the revocation is observed on
    # the registry itself before any further request is admitted.
    server.begin_drain()
    assert server._reconciliation_sessions == {}

    successor = _server(_session_ready(), invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    r = await _recon(
        successor,
        successor._session.authority_instance_id,
        "reconciliation_list",
        {"reconciliation_session_id": wire_id},
    )
    assert r["error"]["code"] == "reconciliation_session_unknown"


async def test_reconciliation_session_registry_bounded() -> None:
    from webwire.authority_ipc_server import IPC_MAX_RECONCILIATION_SESSIONS

    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    instance = session.authority_instance_id
    for _ in range(IPC_MAX_RECONCILIATION_SESSIONS):
        assert (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["ok"] is True
    r = await _recon(server, instance, "reconciliation_open", {"operator_id": "op"})
    assert r["ok"] is False
    assert r["error"]["code"] == "reconciliation_busy"


async def test_reconciliation_without_provider_refused() -> None:
    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=None)
    r = await _recon(server, session.authority_instance_id, "reconciliation_open", {"operator_id": "op"})
    assert r["ok"] is False
    assert r["error"]["code"] == "reconciliation_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"effect_id": "fx-1", "verdict": "NOT_A_VERDICT", "evidence": {}, "evidence_summary": "s"},
        {"effect_id": "fx-1", "verdict": "CONFIRMED_EFFECT", "evidence_summary": "s"},  # no evidence
        {
            "effect_id": "fx-1",
            "verdict": "CONFIRMED_EFFECT",
            "evidence": {},
            "evidence_summary": "",
        },  # empty summary
    ],
)
async def test_reconciliation_prepare_schema_strict(payload: dict) -> None:
    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    instance = session.authority_instance_id
    wire_id = (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
        "reconciliation_session_id"
    ]
    payload["reconciliation_session_id"] = wire_id
    r = await _recon(server, instance, "reconciliation_prepare", payload)
    assert r["ok"] is False
    assert r["error"]["code"] == "schema"


# ---------------------------------------------------------------------------
# F-77: the mandatory safety envelope survives oversized diagnostics
# ---------------------------------------------------------------------------


def _oversized_uncertain_failure() -> Any:
    """An uncertain-effect failure whose projected diagnostic data is
    deliberately enormous — a 32-wide, 4-deep nested structure (over a
    million bounded strings) that dwarfs the 1 MiB response ceiling."""

    def _grow(_depth: int) -> Any:
        # Hundreds of 512-char strings in nested lists: every per-node cap
        # (items/depth/length) is satisfied, yet the projected optional
        # block is ~131 KiB — over the 64 KiB total diagnostic budget.
        return {"blobs": [["x" * 512 for _ in range(32)] for _ in range(8)]}

    result = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.UNKNOWN,
            message="submit clicked; verification pending",
            recoverable=False,
        ),
    )
    result.data = {
        "policy": {"verdict": "allow", "blocked_by": None},
        "trace": {"stages": ["intent_created", "submit_clicked_verification_pending"]},
        "public_side_effect": True,
        "reconciliation_required": True,
        "m5_effect_state": "effect_unknown",
        "semantic_key": "actor|post|none|big",
        "data": {"diagnostic_blobs": _grow(4), "failure_code": "submit_clicked_verification_pending"},
    }
    return result


async def test_oversized_failure_still_delivers_mandatory_safety_facts() -> None:
    """F-77: when the projected diagnostics would overflow the response
    ceiling, the degradation ladder drops them and the THREE MANDATORY
    safety facts (plus the semantic key) still reach the client — never
    a generic internal."""
    session = _session_ready()

    async def invoke(name: str, payload: dict) -> Any:
        return _oversized_uncertain_failure()

    server = _server(session, invoke)
    frame = _frame("post_text", {"text": "big"}, instance_id=session.authority_instance_id)
    response = await _respond(server, frame)

    assert response["ok"] is False
    assert response["error"]["code"] == "capability"
    safety = response["safety"]
    assert safety["public_side_effect"] is True
    assert safety["reconciliation_required"] is True
    assert safety["m5_effect_state"] == "effect_unknown"
    assert safety["semantic_key"] == "actor|post|none|big"
    # The oversized optional diagnostics were dropped by the ladder.
    assert "diagnostic_blobs" not in safety.get("data", {})


def test_unsupported_objects_redact_not_repr() -> None:
    """F-77: unsupported object types in projected data reduce to a
    STABLE REDACTION MARKER naming the type — a repr could leak
    authority-object state across the boundary."""
    from webwire.authority_ipc_server import _json_safe

    class _SecretAuthority:
        def __repr__(self) -> str:  # pragma: no cover - must never reach the wire
            return "SecretAuthority(token='abc123')"

    safe = _json_safe({"authority": _SecretAuthority(), "nested": [_SecretAuthority()]})
    assert safe["authority"] == "<redacted:_SecretAuthority>"
    assert safe["nested"] == ["<redacted:_SecretAuthority>"]


# ---------------------------------------------------------------------------
# F-78: bounded and reclaimable reconciliation resources
# ---------------------------------------------------------------------------


async def test_reconciliation_close_reclaims_the_registry_slot() -> None:
    """F-78: four opens fill the bounded registry; an explicit close
    frees the slot for a fifth open (before drain/restart)."""
    session = _session_ready()
    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _FakeOperatorSession(oid))
    instance = session.authority_instance_id

    ids = []
    for _ in range(4):
        r = await _recon(server, instance, "reconciliation_open", {"operator_id": "op"})
        assert r["ok"] is True
        ids.append(r["data"]["reconciliation_session_id"])
    busy = await _recon(server, instance, "reconciliation_open", {"operator_id": "op"})
    assert busy["error"]["code"] == "reconciliation_busy"

    closed = await _recon(server, instance, "reconciliation_close", {"reconciliation_session_id": ids[0]})
    assert closed["ok"] is True and closed["data"]["closed"] is True
    fifth = await _recon(server, instance, "reconciliation_open", {"operator_id": "op"})
    assert fifth["ok"] is True

    # The closed session is gone: its id is unknown afterwards.
    gone = await _recon(server, instance, "reconciliation_list", {"reconciliation_session_id": ids[0]})
    assert gone["error"]["code"] == "reconciliation_session_unknown"


async def test_reconciliation_prepare_is_capped_per_session() -> None:
    """F-78: the per-session proposal map is bounded — the 33rd prepare
    is refused with a stable code."""
    from webwire.authority_ipc_server import IPC_MAX_RECONCILIATION_PROPOSALS

    session = _session_ready()

    class _ManyProposalSession(_FakeOperatorSession):
        counter = 0

        def prepare_resolution(
            self, *, effect_id: str, verdict: Any, evidence: dict, evidence_summary: str
        ) -> Any:
            _ManyProposalSession.counter += 1
            proposal = super().prepare_resolution(
                effect_id=effect_id, verdict=verdict, evidence=evidence, evidence_summary=evidence_summary
            )
            proposal.proposal_id = f"prop-{_ManyProposalSession.counter}"
            return proposal

    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _ManyProposalSession(oid))
    instance = session.authority_instance_id
    wire_id = (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
        "reconciliation_session_id"
    ]
    for i in range(IPC_MAX_RECONCILIATION_PROPOSALS):
        r = await _recon(
            server,
            instance,
            "reconciliation_prepare",
            {
                "reconciliation_session_id": wire_id,
                "effect_id": "fx-1",
                "verdict": "CONFIRMED_EFFECT",
                "evidence": {"n": i},
                "evidence_summary": f"summary {i}",
            },
        )
        assert r["ok"] is True, r
    overflow = await _recon(
        server,
        instance,
        "reconciliation_prepare",
        {
            "reconciliation_session_id": wire_id,
            "effect_id": "fx-1",
            "verdict": "CONFIRMED_EFFECT",
            "evidence": {"n": 99},
            "evidence_summary": "one too many",
        },
    )
    assert overflow["ok"] is False
    assert overflow["error"]["code"] == "reconciliation_proposal_limit"


async def test_reconciliation_list_is_paginated() -> None:
    """F-78: list returns a bounded page with an explicit limit and
    keyset pagination by effect_id — never an unbounded target list."""
    session = _session_ready()

    class _MultiTargetSession(_FakeOperatorSession):
        def list_targets(self) -> Any:
            self.calls.append("list")
            base = super().list_targets()[0]
            targets = []
            for suffix in ("a", "b", "c"):
                target = type("T", (), {})()
                target.effect_id = f"fx-1-{suffix}"
                target.first_record = base.first_record
                target.last_record = base.last_record
                targets.append(target)
            return tuple(targets)

    server = _server(session, invoke=_noop_invoke, provider=lambda oid: _MultiTargetSession(oid))
    instance = session.authority_instance_id
    wire_id = (await _recon(server, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
        "reconciliation_session_id"
    ]

    page1 = await _recon(
        server,
        instance,
        "reconciliation_list",
        {"reconciliation_session_id": wire_id, "limit": 2},
    )
    assert page1["ok"] is True
    assert [t["effect_id"] for t in page1["data"]["targets"]] == ["fx-1-a", "fx-1-b"]
    assert page1["data"]["total"] == 3  # TRUE total unresolved, not after-cursor
    assert page1["data"]["remaining"] == 3
    assert page1["data"]["limit"] == 2
    assert page1["data"]["has_more"] is True

    page2 = await _recon(
        server,
        instance,
        "reconciliation_list",
        {"reconciliation_session_id": wire_id, "limit": 2, "after_effect_id": "fx-1-b"},
    )
    assert [t["effect_id"] for t in page2["data"]["targets"]] == ["fx-1-c"]
    assert page2["data"]["total"] == 3  # true total is cursor-independent
    assert page2["data"]["remaining"] == 1
    assert page2["data"]["has_more"] is False

# ---------------------------------------------------------------------------
# F-80: the safety envelope is size-independent — even a >1 MiB message
# ---------------------------------------------------------------------------


async def test_giant_error_message_still_delivers_mandatory_safety() -> None:
    """F-80: a capability error message LARGER than the response ceiling
    cannot push every degradation rung over the limit — the message is
    bounded at each rung and the absolute floor (synthetic message +
    independently extracted mandatory facts) always encodes. The response
    decodes normally, carries the code and mandatory safety facts, is
    never a generic internal, and the same request_id returns the
    retained identical frame."""
    session = _session_ready()
    calls: list = []

    def _giant_failure() -> Any:
        result = action_result(
            ok=False,
            error=ActionError(
                category=ErrorCategory.UNKNOWN,
                message="x" * (2 * 1024 * 1024),  # 2 MiB — over every ceiling
                recoverable=False,
            ),
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": "effect_unknown",
            "semantic_key": "actor|post|none|giant",
        }
        return result

    async def invoke(name: str, payload: dict) -> Any:
        calls.append(name)
        return _giant_failure()

    server = _server(session, invoke)
    rid = secrets.token_hex(16)
    frame = _frame("post_text", {"text": "boom"}, instance_id=session.authority_instance_id, request_id=rid)

    response = await _respond(server, frame)
    assert response["ok"] is False
    assert response["error"]["code"] == "capability"
    assert len(response["error"]["message"]) <= 2048, "the wire message is bounded"
    safety = response["safety"]
    assert safety["public_side_effect"] is True
    assert safety["reconciliation_required"] is True
    assert safety["m5_effect_state"] == "effect_unknown"
    assert safety["semantic_key"] == "actor|post|none|giant"

    # The retained retry is identical — no re-execution.
    second = await _respond(server, frame)
    assert second == response
    assert len(calls) == 1
