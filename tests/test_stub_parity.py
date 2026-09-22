"""Stub/SDK parity (runs ONLY where the real SDK is installed — i.e., dev
machines, not CI). If the real Super-Browser SDK drifts, this fails locally
first, before CI's stub could silently diverge from reality."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

real_sb = pytest.importorskip("super_browser")  # skip in CI (stub-only)

STUB_ROOT = Path(__file__).parent / "stubs"


def _load_stub_module(dotted: str):
    """Import a stub module under an alias without disturbing the real one.
    The alias MUST be registered in sys.modules before exec — dataclass field
    resolution looks up the class's module there."""
    rel = dotted.replace(".", "/")
    path = STUB_ROOT / rel
    path = path.with_suffix(".py") if (STUB_ROOT / f"{rel}.py").exists() else path / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"stub_{dotted}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_enums_match() -> None:
    from super_browser.results import types as real_types

    stub_types = _load_stub_module("super_browser.results.types")
    for enum_name in ("SuccessCategory", "FailureCategory", "ErrorCategory"):
        real = getattr(real_types, enum_name)
        stub = getattr(stub_types, enum_name)
        assert {m.name for m in real} == {m.name for m in stub}, (
            f"{enum_name} members diverge: real={sorted(m.name for m in real)} "
            f"stub={sorted(m.name for m in stub)}"
        )
        for m in real:
            assert m.value == getattr(stub, m.name).value, (
                f"{enum_name}.{m.name} value drift: real={m.value!r}"
            )


def test_action_error_fields_match() -> None:
    from super_browser.results import types as real_types

    stub_types = _load_stub_module("super_browser.results.types")
    real_fields = {f for f in real_types.ActionError.__dataclass_fields__}
    stub_fields = {f for f in stub_types.ActionError.__dataclass_fields__}
    # Every field the real ActionError has must exist on the stub (the stub
    # may add none; extras on the real side are the drift we care about).
    assert real_fields - stub_fields == set(), (
        f"stub ActionError missing fields: {real_fields - stub_fields}"
    )


def test_action_result_shape_matches() -> None:
    from super_browser.results import types as real_types

    stub_types = _load_stub_module("super_browser.results.types")
    for attrs in ({"ok"}, {"ok", "data"}, {"ok", "error", "data"}):
        r = real_types.action_result(ok=True, data=None) if attrs == {"ok"} else None
    real = real_types.action_result(ok=True, data={"a": 1})
    stub = stub_types.action_result(ok=True, data={"a": 1})
    for field_name in ("ok", "data", "error", "success_category", "failure_category"):
        assert hasattr(real, field_name) and hasattr(stub, field_name), field_name
    # Mutability (the envelope is assigned after construction in webwire).
    stub.failure_category = stub_types.FailureCategory.SECURITY
    assert stub.failure_category.value == "security"


def test_session_config_fields_match() -> None:
    from super_browser.browser.config import SessionConfig as RealSC

    stub_cfg = _load_stub_module("super_browser.browser.config")
    real_fields = set(RealSC.__dataclass_fields__)
    stub_fields = set(stub_cfg.SessionConfig.__dataclass_fields__)
    assert real_fields - stub_fields == set(), (
        f"stub SessionConfig missing fields: {real_fields - stub_fields}"
    )
