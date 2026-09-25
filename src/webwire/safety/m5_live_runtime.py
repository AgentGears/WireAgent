"""Supported live construction for the initial Layer-5 migrated effects.

The stack keeps all authority-bearing and browser-coordination objects explicit
so Dispatcher migration can install one coherent live path instead of
independently constructing pieces that merely look compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.m5_leased_read_broker import M5LeasedReadBroker
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectPolicyRegistry
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_actor_bound_evidence import M5ActorBoundEvidenceReader
from webwire.safety.m5_actor_bound_media_executor import M5ActorBoundMediaExecutor
from webwire.safety.m5_actor_bound_post_executor import M5ActorBoundPostTextExecutor
from webwire.safety.m5_authority_factory import (
    build_live_m5_read_broker,
    build_live_m5_write_broker,
    build_live_scoped_authority_broker,
)
from webwire.safety.m5_delete_evidence import M5LeasedDeleteEvidenceReader
from webwire.safety.m5_delete_executor import M5DeleteExecutor
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_media_evidence import M5LeasedMediaEvidenceReader
from webwire.safety.m5_media_executor import M5MediaExecutor
from webwire.safety.m5_post_text_executor import M5PostTextExecutor
from webwire.safety.m5_quote_executor import M5QuoteExecutor
from webwire.safety.m5_reply_executor import M5ReplyExecutor
from webwire.safety.scoped_authority import ScopedAuthorityBroker

__all__ = ["M5LiveExecutionStack", "build_live_m5_execution_stack"]


@dataclass(frozen=True)
class M5LiveExecutionStack:
    """One coherent process-local Layer-5 execution stack."""

    read_broker: M5LeasedReadBroker
    write_broker: M5LeasedWriteBroker
    scoped_authority: ScopedAuthorityBroker
    execution_runtime: M5ExecutionRuntime
    evidence_reader: M5LeasedEvidenceReader
    media_evidence_reader: M5LeasedMediaEvidenceReader
    delete_evidence_reader: M5LeasedDeleteEvidenceReader
    effect_executor: M5EffectExecutor
    post_text_executor: M5PostTextExecutor
    reply_executor: M5ReplyExecutor
    quote_executor: M5QuoteExecutor
    media_executor: M5MediaExecutor
    delete_executor: M5DeleteExecutor


def build_live_m5_execution_stack(
    super_browser: Any,
    kill_switch: KillSwitch,
    commit_gateway: CommitGateway,
    *,
    config: Optional[WebWireConfig] = None,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
) -> M5LiveExecutionStack:
    """Build the supported live stack from one browser/gateway authority root."""

    read_broker = build_live_m5_read_broker(
        super_browser,
        kill_switch,
        config=config,
    )
    write_broker = build_live_m5_write_broker(super_browser, kill_switch)
    if read_broker._m5_write_state is not write_broker._m5_write_state:
        raise RuntimeError("live M5 read/write brokers do not share browser lease state")
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
    # Supported live content verification is actor-bound.  The generic reader
    # remains available to isolated tests, but live post/media confirmation must
    # prove direct status ownership and return the observed actor.
    evidence_reader = M5ActorBoundEvidenceReader(write_broker)
    media_evidence_reader = M5LeasedMediaEvidenceReader(write_broker)
    delete_evidence_reader = M5LeasedDeleteEvidenceReader(write_broker)
    effect_executor = M5EffectExecutor(
        runtime=execution_runtime,
        evidence_reader=evidence_reader,
    )
    post_text_executor = M5ActorBoundPostTextExecutor(
        runtime=execution_runtime,
        evidence_reader=evidence_reader,
    )
    reply_executor = M5ReplyExecutor(
        runtime=execution_runtime,
        evidence_reader=evidence_reader,
    )
    quote_executor = M5QuoteExecutor(
        runtime=execution_runtime,
        evidence_reader=evidence_reader,
    )
    media_executor = M5ActorBoundMediaExecutor(
        runtime=execution_runtime,
        content_evidence=evidence_reader,
        media_evidence=media_evidence_reader,
    )
    delete_executor = M5DeleteExecutor(
        runtime=execution_runtime,
        evidence_reader=delete_evidence_reader,
    )
    return M5LiveExecutionStack(
        read_broker=read_broker,
        write_broker=write_broker,
        scoped_authority=scoped_authority,
        execution_runtime=execution_runtime,
        evidence_reader=evidence_reader,
        media_evidence_reader=media_evidence_reader,
        delete_evidence_reader=delete_evidence_reader,
        effect_executor=effect_executor,
        post_text_executor=post_text_executor,
        reply_executor=reply_executor,
        quote_executor=quote_executor,
        media_executor=media_executor,
        delete_executor=delete_executor,
    )
