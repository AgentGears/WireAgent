"""Dispatcher-level tests for the M5-migrated ``post_text`` capability."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from webwire.broker import ReadOnlyBroker
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.safety.effect_ledger import EffectState
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_post_text_executor import M5PostTextExecutor
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True
        self.set_resolved_handle("@actor")


class _FakePostBroker:
    """Scoped content broker used by the Dispatcher migration tests."""

    def __init__(self, composer_text_after_fill: str | None = None) -> None:
        self._composer_text_after_fill = composer_text_after_fill
        self._composer_text = ""
        self.fill_calls: list[str] = []
        self.submit_clicked = False
        self.cleanup_calls = 0
        self.gate_calls = 0

    async def fill_composer(self, text: str) -> ActionResult:
        self.fill_calls.append(text)
        self._composer_text = text
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        text = (
            self._composer_text_after_fill
            if self._composer_text_after_fill is not None
            else self._composer_text
        )
        return ok_result(data={"composer_text": text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError(f"plain post must not attach media: {image_path}")

    async def close_composer(self) -> ActionResult:
        self.cleanup_calls += 1
        self._composer_text = ""
        return ok_result(data={"closed": True})

    async def click_submit(
        self,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
        _precommit_check,  # type: ignore[no-untyped-def]
        _expected_text: str,
        _expected_attachments: int,
    ) -> ActionResult:
        assert _expected_text == self._composer_text
        assert _expected_attachments == 0
        checked = await _precommit_check()
        if checked is not None:
            return checked
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.submit_clicked = True
        return ok_result(data={"submitted": True})


class _Evidence:
    async def capture_pre_submit_ids(self) -> ActionResult:
        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        assert pre_submit_ids == {"10", "11"}
        assert exclude_ids in (None, set())
        return ok_result(
            data={
                "post_id": "123",
                "post_url": "https://x.com/actor/status/123",
            }
        )

    async def verify_post_text(
        self,
        post_url: str,
        normalized_text: str,
    ) -> ActionResult:
        assert post_url == "https://x.com/actor/status/123"
        return ok_result(
            data={
                "text_matches": True,
                "normalized_text": normalized_text,
            }
        )


def _make_dispatcher(
    tmp_path: Path,
    fake_broker: _FakePostBroker,
) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    dispatcher._broker = ReadOnlyBroker(  # type: ignore[arg-type]
        session.sb,
        dispatcher._kill,
        cfg,
    )
    scoped = ScopedAuthorityBroker(
        fake_broker,
        dispatcher._m5_gateway,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=dispatcher._m5_gateway,
    )
    executor = M5PostTextExecutor(
        runtime=runtime,
        evidence_reader=_Evidence(),
    )
    dispatcher._m5_stack = SimpleNamespace(  # type: ignore[assignment]
        post_text_executor=executor,
    )
    dispatcher._m5_post_text_adapter = None
    dispatcher._write_kernel._write_broker_factory = lambda: object()  # type: ignore[attr-defined]
    return dispatcher


async def _confirm(
    dispatcher: Dispatcher,
    text: str,
) -> str:
    result = await dispatcher.invoke("post_text", {"text": text})
    assert result.ok is True
    assert result.data["policy"]["verdict"] == "confirmation_required"
    return result.data["data"]["confirmation_token"]


async def test_post_text_first_invoke_confirmation_required(tmp_path: Path) -> None:
    fake = _FakePostBroker()
    dispatcher = _make_dispatcher(tmp_path, fake)

    result = await dispatcher.invoke("post_text", {"text": "Hello world"})

    assert result.ok is True
    assert result.data["policy"]["verdict"] == "confirmation_required"
    assert "confirmation_token" in result.data["data"]
    assert fake.submit_clicked is False


async def test_post_text_preview_warns_irreversible(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, _FakePostBroker())

    result = await dispatcher.invoke("post_text", {"text": "Test"})

    warnings = result.data["data"].get("warnings", [])
    preview = result.data["data"].get("preview", "")
    combined = preview + " ".join(warnings)
    assert "IRREVERSIBLE" in combined or "irreversible" in combined


async def test_post_text_composer_mismatch_aborts_before_submit(tmp_path: Path) -> None:
    fake = _FakePostBroker(composer_text_after_fill="DIFFERENT TEXT")
    dispatcher = _make_dispatcher(tmp_path, fake)
    token = await _confirm(dispatcher, "Expected text")

    result = await dispatcher.invoke(
        "post_text",
        {"text": "Expected text", "confirmation_token": token},
    )

    assert result.ok is False
    assert result.data["policy"]["verdict"] == "deny"
    assert result.data["trace"]["execute_ok"] is False
    assert fake.submit_clicked is False
    assert fake.cleanup_calls == 1
    assert dispatcher._m5_ledger.read_records() == []


async def test_post_text_matching_composer_records_confirmed_effect(tmp_path: Path) -> None:
    fake = _FakePostBroker()
    dispatcher = _make_dispatcher(tmp_path, fake)
    token = await _confirm(dispatcher, "Hello world")

    result = await dispatcher.invoke(
        "post_text",
        {"text": "Hello world", "confirmation_token": token},
    )

    assert result.data["policy"]["verdict"] == "allow"
    assert result.data["trace"]["execute_ok"] is True
    assert result.data["trace"]["verify_ok"] is True
    assert fake.submit_clicked is True
    assert fake.gate_calls == 1
    assert [record.state for record in dispatcher._m5_ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]


async def test_post_text_kill_before_second_invoke_aborts(tmp_path: Path) -> None:
    fake = _FakePostBroker()
    dispatcher = _make_dispatcher(tmp_path, fake)
    token = await _confirm(dispatcher, "test")
    dispatcher.kill_switch.trip()

    result = await dispatcher.invoke(
        "post_text",
        {"text": "test", "confirmation_token": token},
    )

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert fake.submit_clicked is False
    assert dispatcher._m5_ledger.read_records() == []


async def test_post_text_token_binds_text(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, _FakePostBroker())
    token = await _confirm(dispatcher, "Text A")

    result = await dispatcher.invoke(
        "post_text",
        {"text": "Text B", "confirmation_token": token},
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "intent_mismatch"
    assert dispatcher._m5_ledger.read_records() == []


async def test_post_text_dedupe_blocks_confirmed_replay(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, _FakePostBroker())
    token = await _confirm(dispatcher, "test post")
    executed = await dispatcher.invoke(
        "post_text",
        {"text": "test post", "confirmation_token": token},
    )
    assert executed.ok is True

    replay = await dispatcher.invoke("post_text", {"text": "test post"})

    assert replay.ok is False
    assert replay.data["policy"]["blocked_by"] == "dedupe"


async def test_post_text_dedupe_key_contains_normalized_text_hash(tmp_path: Path) -> None:
    from webwire.safety.text_normalize import text_hash

    dispatcher = _make_dispatcher(tmp_path, _FakePostBroker())
    result = await dispatcher.invoke("post_text", {"text": "test post"})

    dedupe_key = result.data["trace"]["intent"]["dedupe_key"]
    assert text_hash("test post") in dedupe_key


async def test_post_text_normalizes_before_scoped_fill(tmp_path: Path) -> None:
    fake = _FakePostBroker()
    dispatcher = _make_dispatcher(tmp_path, fake)
    token = await _confirm(dispatcher, "  hello   world  ")

    result = await dispatcher.invoke(
        "post_text",
        {"text": "  hello   world  ", "confirmation_token": token},
    )

    assert result.ok is True
    assert fake.fill_calls == ["hello world"]


async def test_post_text_dispatcher_kill_blocks(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, _FakePostBroker())
    dispatcher.kill_switch.trip()

    result = await dispatcher.invoke("post_text", {"text": "test"})

    assert result.ok is False
    assert result.failure_category.value == "security"
