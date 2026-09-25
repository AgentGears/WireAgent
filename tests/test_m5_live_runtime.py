"""Construction invariants for the Layer-5 live execution stack."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.m5_leased_read_broker import M5LeasedReadBroker
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_live_runtime import build_live_m5_execution_stack
from webwire.safety.m5_media_evidence import M5LeasedMediaEvidenceReader
from webwire.safety.m5_media_executor import M5MediaExecutor
from webwire.safety.m5_post_text_executor import M5PostTextExecutor
from webwire.safety.m5_quote_executor import M5QuoteExecutor
from webwire.safety.m5_reply_executor import M5ReplyExecutor
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied


class _SB:
    pass


def _gateway(tmp_path: Path, kill: KillSwitch) -> CommitGateway:
    return CommitGateway(
        ledger=EffectLedger(WebWireConfig(state_dir=tmp_path, kill_env_var=None)),
        kill_switch=kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )


def test_live_stack_uses_one_shared_authority_root(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    gateway = _gateway(tmp_path, kill)

    stack = build_live_m5_execution_stack(_SB(), kill, gateway, config=cfg)

    assert isinstance(stack.read_broker, M5LeasedReadBroker)
    assert isinstance(stack.write_broker, M5LeasedWriteBroker)
    assert isinstance(stack.scoped_authority, ScopedAuthorityBroker)
    assert isinstance(stack.execution_runtime, M5ExecutionRuntime)
    assert isinstance(stack.evidence_reader, M5LeasedEvidenceReader)
    assert isinstance(stack.media_evidence_reader, M5LeasedMediaEvidenceReader)
    assert isinstance(stack.effect_executor, M5EffectExecutor)
    assert isinstance(stack.post_text_executor, M5PostTextExecutor)
    assert isinstance(stack.reply_executor, M5ReplyExecutor)
    assert isinstance(stack.quote_executor, M5QuoteExecutor)
    assert isinstance(stack.media_executor, M5MediaExecutor)
    assert stack.read_broker._kill is kill
    assert stack.write_broker._kill is kill
    assert gateway._kill is kill
    assert stack.read_broker._sb is stack.write_broker._sb
    assert stack.read_broker._m5_write_state is stack.write_broker._m5_write_state
    assert stack.execution_runtime._gateway is gateway
    assert stack.execution_runtime._scoped is stack.scoped_authority
    assert stack.effect_executor._runtime is stack.execution_runtime
    assert stack.post_text_executor._runtime is stack.execution_runtime
    assert stack.reply_executor._runtime is stack.execution_runtime
    assert stack.quote_executor._runtime is stack.execution_runtime
    assert stack.media_executor._runtime is stack.execution_runtime
    assert stack.effect_executor._evidence is stack.evidence_reader
    assert stack.post_text_executor._evidence is stack.evidence_reader
    assert stack.reply_executor._evidence is stack.evidence_reader
    assert stack.quote_executor._evidence is stack.evidence_reader
    assert stack.media_executor._content_evidence is stack.evidence_reader
    assert stack.media_executor._media_evidence is stack.media_evidence_reader
    assert stack.media_evidence_reader._M5LeasedMediaEvidenceReader__broker is stack.write_broker


def test_repeated_live_stack_for_same_facade_shares_browser_lease_state(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    gateway = _gateway(tmp_path, kill)
    sb = _SB()

    first = build_live_m5_execution_stack(sb, kill, gateway, config=cfg)
    second = build_live_m5_execution_stack(sb, kill, gateway, config=cfg)

    assert first.read_broker._sb is first.write_broker._sb
    assert second.read_broker._sb is second.write_broker._sb
    assert first.write_broker._sb is second.write_broker._sb
    assert first.read_broker._m5_write_state is first.write_broker._m5_write_state
    assert first.read_broker._m5_write_state is second.read_broker._m5_write_state
    assert first.write_broker._m5_write_state is second.write_broker._m5_write_state


def test_live_stack_rejects_broker_gateway_kill_identity_mismatch(
    tmp_path: Path,
) -> None:
    cfg_a = WebWireConfig(state_dir=tmp_path / "a", kill_env_var=None)
    cfg_b = WebWireConfig(state_dir=tmp_path / "b", kill_env_var=None)
    broker_kill = KillSwitch(cfg_a)
    gateway_kill = KillSwitch(cfg_b)
    gateway = _gateway(tmp_path / "b", gateway_kill)

    with pytest.raises(ScopedAuthorityDenied, match="broker_contract_mismatch"):
        build_live_m5_execution_stack(_SB(), broker_kill, gateway, config=cfg_a)
