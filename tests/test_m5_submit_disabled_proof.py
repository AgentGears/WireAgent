"""Regression for fail-closed submit-button disabled-state proof."""

from webwire.m5_scoped_write_broker import M5ScopedWriteBroker


def test_submit_proof_rejects_dom_and_aria_disabled_controls() -> None:
    for click in (False, True):
        expr = M5ScopedWriteBroker._content_proof_js(
            "ctx",
            "post",
            "none",
            "approved",
            0,
            click=click,
            submit_token="submit-token",
        )
        assert "btn.disabled||btn.getAttribute('aria-disabled')==='true'" in expr
        assert "return 'submit_disabled'" in expr
        assert "btn.getAttribute('disabled')" not in expr
