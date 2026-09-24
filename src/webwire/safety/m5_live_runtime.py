"""Supported live construction for the initial Layer-5 migrated effects.

The stack keeps all authority-bearing objects explicit so Dispatcher migration
can install one coherent live path instead of independently constructing pieces
that merely look compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectPolicyRegistry
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_authority_factory import (
    build_live_m5_write_broker,
    build_live_scoped_authority_broker,
)
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.scoped_authority import ScopedAuthorityBroker

__all__ = ["M5LiveExecutionStack", "build_live_m5_execution_stack"]


@dataclass(frozen=True)
class M5LiveExecutionStack:
    """One coherent process-local Layer-5 execution stack."""

    write_broker: M5LeasedWriteBroker
    scoped_authority: ScopedAuthorityBroker
    execution_runtime: M5ExecutionRuntime
    evidence_reader: M5LeasedEvidenceReader
    effect_executor: M5EffectExecutor


def build_live_m5_execution_stack(
    super_browser: Any,
    kill_switch: KillSwitch,
    commit_gateway: CommitGateway,
    *,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
) -> M5LiveExecutionStack:
    """Build the supported live stack from one browser/gateway authority root."""

    write_broker = build_live_m5_write_broker(super_browser, kill_switch)
    scoped_authority = build_live_scoped_authority_broker(
        write_broker,
        commit_gateway,
        policies=policies,
    )
    execution_runtime = M5ExecutionRuntime(
        scoped_authority=scoped_authority,
        commit_gateway=commit_gateway,
        policies=policies,
    )
    evidence_reader = M5LeasedEvidenceReader(write_broker)
    effect_executor = M5EffectExecutor(
        runtime=execution_runtime,
        evidence_reader=evidence_reader,
    )
    return M5LiveExecutionStack(
        write_broker=write_broker,
        scoped_authority=scoped_authority,
        execution_runtime=execution_runtime,
        evidence_reader=evidence_reader,
        effect_executor=effect_executor,
    )
