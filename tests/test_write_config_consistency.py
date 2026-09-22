"""Config-consistency tests — P0 fixes (2026-09-22).

Fix 1 (rate-limit bypass): the four media capabilities declared action_types
that existed in neither DEFAULT_LIMITS nor the risk registry — the token
bucket's no_limit path let the highest-risk writes bypass per-action budgets.
Fixed by NORMALIZATION: media capabilities use the BASE action_type
(post / reply / quote), so a media post draws from the same budget as a text
post. Registering separate buckets would fragment the budgets and re-create
the mixed-action-loop weakness the bucket's own design notes call out.

Fix 2 (registry gate, gap 3): the kernel now verifies every write against the
risk registry — unknown action_types are denied, and declared risk/compensation
meta must match the registry entry exactly.

These tests make that class of bug permanent-proof: a new WRITE capability
whose action_type lacks a bucket entry or a registry entry fails the suite,
and so does drift between a capability's declared meta and the registry.
"""

from __future__ import annotations

from pathlib import Path

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.safety import DEFAULT_LIMITS, DEFAULT_REGISTRY

# ---------------------------------------------------------------------------
# The permanent guard: every WRITE action_type has bucket + registry entries
# ---------------------------------------------------------------------------

def test_every_write_action_type_has_bucket_and_registry_entry(
    tmp_path: Path, write_sample_inputs,
) -> None:
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    samples = write_sample_inputs

    write_caps = [
        name for name in d.capabilities
        if d._registry.get(name).tier == CapabilityTier.WRITE
    ]
    assert write_caps, "sanity: the registry must contain WRITE capabilities"

    missing_samples = [n for n in write_caps if n not in samples]
    assert not missing_samples, (
        f"WRITE capabilities without sample inputs in tests/conftest.py: {missing_samples}. "
        "Add a sample so the consistency check can compose them — the test exists "
        "to force every write action_type to be consciously registered."
    )

    problems: list[str] = []
    for name in write_caps:
        cap = d._registry.get(name)
        intent = cap.compose(samples[name], actor_identity="tester")
        if intent.action_type not in DEFAULT_LIMITS:
            problems.append(f"{name}: action_type {intent.action_type!r} has NO TokenBucket entry")
        if DEFAULT_REGISTRY.get(intent.action_type) is None:
            problems.append(f"{name}: action_type {intent.action_type!r} has NO risk-registry entry")
    assert not problems, (
        "Unregistered write action_types (the media rate-limit bypass class): "
        + "; ".join(problems)
    )


# ---------------------------------------------------------------------------
# Registry-gate consistency (gap 3): declared meta MUST equal the registry
# ---------------------------------------------------------------------------

def test_every_write_capability_meta_matches_registry(
    tmp_path: Path, write_sample_inputs,
) -> None:
    """The kernel now denies writes whose declared risk/compensation meta
    differs from the registry. This test guarantees no REAL capability trips
    that gate: composed intents must match the registry byte-for-byte."""
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    for name in d.capabilities:
        cap = d._registry.get(name)
        if cap.tier != CapabilityTier.WRITE:
            continue
        intent = cap.compose(write_sample_inputs[name], actor_identity="tester")
        reg_meta, reg_comp = DEFAULT_REGISTRY.get(intent.action_type)
        assert intent.risk_meta == reg_meta, (
            f"{name}: declared RiskMeta drifts from the registry entry for "
            f"{intent.action_type!r} — the kernel would deny this write"
        )
        assert intent.compensation == reg_comp, (
            f"{name}: declared CompensationMeta drifts from the registry entry for "
            f"{intent.action_type!r} — the kernel would deny this write"
        )


# ---------------------------------------------------------------------------
# Normalization: media caps use base actions and stay semantically distinct
# ---------------------------------------------------------------------------

def test_media_caps_normalize_to_base_actions(tmp_path: Path, write_sample_inputs) -> None:
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    expected = {
        "post_photo": "post",
        "post_multi_image": "post",
        "reply_photo": "reply",
        "quote_photo": "quote",
        "reply_multi_image": "reply",
        "quote_multi_image": "quote",
    }
    for cap_name, base in expected.items():
        intent = d._registry.get(cap_name).compose(write_sample_inputs[cap_name], actor_identity="t")
        assert intent.action_type == base, (
            f"{cap_name} must normalize to base action {base!r} so it draws from "
            f"that action's budget (got {intent.action_type!r})"
        )


def test_media_dedupe_keys_stay_distinct(tmp_path: Path, write_sample_inputs) -> None:
    """Normalization must not collapse semantic identity: same text, different
    media (or no media) must produce different dedupe keys."""
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    reg = d._registry

    text_only = reg.get("post_text").compose({"text": "hello"}, actor_identity="t")
    photo_red = reg.get("post_photo").compose(write_sample_inputs["post_photo"], actor_identity="t")
    photo_blue = reg.get("post_photo").compose(
        {"text": "hello", "image_path": str(tmp_path / "blue.png")}, actor_identity="t",
    )
    multi = reg.get("post_multi_image").compose(
        write_sample_inputs["post_multi_image"], actor_identity="t",
    )

    keys = {
        ("post_text", "hello"): text_only.dedupe_key(),
        ("post_photo", "red"): photo_red.dedupe_key(),
        ("post_photo", "blue"): photo_blue.dedupe_key(),
        ("post_multi_image", "red+blue"): multi.dedupe_key(),
    }
    values = list(keys.values())
    assert len(set(values)) == len(values), f"dedupe keys collapsed: {keys}"


def test_media_counts_against_base_budget(tmp_path: Path, write_sample_inputs) -> None:
    """End-to-end consequence of normalization: three photo/multi-image post
    intents exhaust the 3-per-hour post budget — previously they were free."""
    from webwire.safety import RiskTier, TokenBucket

    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    reg = d._registry

    bucket = TokenBucket()
    for cap_name in ("post_text", "post_photo", "post_multi_image"):
        intent = reg.get(cap_name).compose(write_sample_inputs[cap_name], actor_identity="t")
        # This is exactly the kernel's step 3a acquire.
        allowed, reason = bucket.acquire(intent.action_type, intent.risk_tier())
        assert allowed, f"{cap_name} should be within budget: {reason}"

    allowed, reason = bucket.acquire("post", RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert not allowed, "a 4th post (any kind) within the hour must be denied"
    assert "token_bucket(post)" in reason
