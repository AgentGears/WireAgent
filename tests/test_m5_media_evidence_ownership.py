"""Adversarial ownership regression for Layer-5 media evidence."""

from __future__ import annotations

from typing import Any

from webwire.envelope import ActionResult, ok_result
from webwire.safety.media_verify import count_post_media


class _CDP:
    def __init__(self) -> None:
        self.expressions: list[str] = []

    async def evaluate(self, expr: str) -> ActionResult:
        self.expressions.append(expr)
        return ok_result(data={"result": {"value": 1}})


class _Controller:
    def __init__(self) -> None:
        self._cdp = _CDP()


class _SB:
    def __init__(self) -> None:
        self._controller = _Controller()
        self.navigations: list[str] = []

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.navigations.append(url)
        return ok_result(data={"url": url, "wait_until": wait_until})


class _Broker:
    def __init__(self) -> None:
        self._sb = _SB()


async def test_media_count_is_scoped_to_direct_outer_post_ownership() -> None:
    broker = _Broker()

    count = await count_post_media(  # type: ignore[arg-type]
        broker,
        "https://x.com/Actor/status/999",
    )

    assert count == 1
    assert broker._sb.navigations == ["https://x.com/Actor/status/999"]
    assert len(broker._sb._controller._cdp.expressions) == 1
    expr = broker._sb._controller._cdp.expressions[0]

    # The captured article must own the requested status through a direct
    # timestamp link; a nested quote/status link cannot select the article.
    assert "a.closest('article')!==art" in expr
    assert "!a.querySelector('time')" in expr
    assert "m&&m[1]===target" in expr

    # Only photos owned by that same article count. A quoteTweet subtree or a
    # nested article can never supply a missing approved outer attachment.
    assert "photo.closest('article')!==art" in expr
    assert "photo.closest(\"[data-testid='quoteTweet']\")" in expr
    assert "if(quote&&art.contains(quote))continue" in expr
    assert "querySelectorAll(\"[data-testid='tweetPhoto']\").length" not in expr
