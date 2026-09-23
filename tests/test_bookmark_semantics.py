"""Bookmark semantic tests (2026-09-23, pre-M5 commit 1).

Drives the REAL WriteBroker over a page model whose selector semantics
include both observed DOM topologies:
  - coexist (2026-09-23, live-probed): 'bookmark' present in both states;
    'removeBookmark' present iff bookmarked; bookmark click is a no-op
    when already bookmarked.
  - swap (the July-era assumption): 'bookmark' present iff NOT bookmarked;
    'removeBookmark' present iff bookmarked.

The defect being locked out: the pre-fix click_bookmark used the
removeBookmark click as a fallback probe — a mutation-as-probe that would
REMOVE the bookmark on an already-bookmarked post and report
"already_bookmarked" success. Latent on today's DOM; re-armable by any
selector drift.

Contract under test (four observable rows + mutual exclusion + the
semantic retry property):
  not_bookmarked → click bookmark
  bookmarked     → already_satisfied, ZERO mutation
  coexist+bookmarked → already_satisfied, ZERO mutation
  unknown state  → fail honestly, ZERO mutation
  click_bookmark MUST NEVER click removeBookmark (and vice versa)
  pre=bookmarked → invoke → post=bookmarked
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ok_result, soft_failure
from webwire.safety import KillSwitch
from webwire.write_broker import WriteBroker

URL = "https://x.com/infaag/status/2102451358305771541"
BM = "[data-testid='bookmark']"
RBM = "[data-testid='removeBookmark']"


class BookmarkPage:
    """DOM model: state + topology determine selector presence and click
    effects. The state probe (rbm checked first) mirrors the real JS."""

    def __init__(self, state: str = "not_bookmarked", topology: str = "coexist",
                 probe_degraded: bool = False) -> None:
        self.state = state
        self.topology = topology          # "coexist" | "swap"
        self.probe_degraded = probe_degraded  # state probe returns 'unknown'
        self.clicks: list[str] = []

    def present(self, sel: str) -> bool:
        is_remove = "removeBookmark" in sel
        if self.topology == "swap":
            return self.state == "bookmarked" if is_remove else self.state == "not_bookmarked"
        # coexist: bookmark present always; removeBookmark iff bookmarked.
        return self.state == "bookmarked" if is_remove else True

    def probe(self) -> str:
        if self.probe_degraded:
            return "unknown"
        if self.present(RBM):
            return "bookmarked"
        if self.present(BM):
            return "not_bookmarked"
        return "unknown"

    def click(self, sel: str) -> Any:
        self.clicks.append(sel)
        if not self.present(sel):
            return soft_failure(f"selector absent: {sel}")
        if "removeBookmark" in sel:
            if self.state == "bookmarked":
                self.state = "not_bookmarked"
        else:
            if self.state == "not_bookmarked":
                self.state = "bookmarked"
            # click-when-bookmarked: observed no-op (2026-09-23 live probe)
        return ok_result(data={"clicked": True})


class _FakeCDP:
    def __init__(self, page: BookmarkPage) -> None:
        self._page = page

    async def evaluate(self, expr: str) -> Any:
        if "removeBookmark" in expr and "bookmark" in expr:
            return ok_result(data={"result": {"value": self._page.probe()}})
        return ok_result(data={"result": {"value": None}})


class _FakeController:
    def __init__(self, page: BookmarkPage) -> None:
        self._cdp = _FakeCDP(page)


class _FakeSB:
    def __init__(self, page: BookmarkPage) -> None:
        self._page = page
        self._controller = _FakeController(page)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> Any:
        return ok_result(data={"url": url})

    async def click(self, sel: str, description: str = "") -> Any:
        return self._page.click(sel)


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(WriteBroker, "_BOOKMARK_SETTLE_S", 0.01)


def _broker(tmp_path: Path, page: BookmarkPage) -> WriteBroker:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteBroker(_FakeSB(page), KillSwitch(cfg))  # type: ignore[arg-type]


async def test_not_bookmarked_clicks_bookmark_once(tmp_path: Path) -> None:
    page = BookmarkPage(state="not_bookmarked", topology="coexist")
    r = await _broker(tmp_path, page).click_bookmark(URL)
    assert r.ok is True
    assert (r.data or {}).get("bookmarked") is True
    assert page.clicks == [BM]
    assert page.state == "bookmarked"


async def test_bookmarked_coexist_zero_mutation(tmp_path: Path) -> None:
    """Today's DOM: both selectors exist; the answer is already_satisfied
    with ZERO clicks — not the primary-click no-op the live probe observed,
    but the stronger code-owned guarantee."""
    page = BookmarkPage(state="bookmarked", topology="coexist")
    r = await _broker(tmp_path, page).click_bookmark(URL)
    assert r.ok is True
    assert (r.data or {}).get("result") == "already_satisfied"
    assert page.clicks == [], "bookmarked state must produce zero mutation"
    assert page.state == "bookmarked"


async def test_bookmarked_swap_zero_mutation_the_regression(tmp_path: Path) -> None:
    """THE regression: July topology (bookmark selector absent when
    bookmarked). The old code fell through to clicking removeBookmark —
    removing the bookmark and reporting already_bookmarked."""
    page = BookmarkPage(state="bookmarked", topology="swap")
    r = await _broker(tmp_path, page).click_bookmark(URL)
    assert r.ok is True
    assert (r.data or {}).get("result") == "already_satisfied"
    assert page.clicks == [], "no mutation-as-probe, ever"
    assert page.state == "bookmarked", "the bookmark survives the no-op"


async def test_unknown_state_fails_honestly_zero_mutation(tmp_path: Path) -> None:
    page = BookmarkPage(state="bookmarked", topology="coexist", probe_degraded=True)
    r = await _broker(tmp_path, page).click_bookmark(URL)
    assert r.ok is False
    assert "unresolved bookmark state" in (getattr(r.error, "message", "") or "")
    assert page.clicks == []
    assert page.state == "bookmarked"


async def test_remove_bookmark_mirror_never_clicks_bookmark(tmp_path: Path) -> None:
    page = BookmarkPage(state="not_bookmarked", topology="coexist")
    r = await _broker(tmp_path, page).click_remove_bookmark(URL)
    assert r.ok is True
    assert (r.data or {}).get("result") == "already_satisfied"
    assert page.clicks == []

    page2 = BookmarkPage(state="bookmarked", topology="swap")
    r2 = await _broker(tmp_path, page2).click_remove_bookmark(URL)
    assert r2.ok is True
    assert page2.clicks == [RBM]
    assert page2.state == "not_bookmarked"


async def test_semantic_retry_property_bookmark_survives_replay(tmp_path: Path) -> None:
    """The property that survives selector churn: pre=bookmarked → invoke
    click_bookmark → post=bookmarked, mutation_count=0. This is the test
    that stays valid whatever X does to selectors next."""
    for topology in ("coexist", "swap"):
        page = BookmarkPage(state="bookmarked", topology=topology)
        broker = _broker(tmp_path, page)
        pre = (await broker.read_bookmark_state(URL)).data["bookmark_state"]
        r = await broker.click_bookmark(URL)
        post = (await broker.read_bookmark_state(URL)).data["bookmark_state"]
        assert (pre, post, page.clicks, r.ok) == ("bookmarked", "bookmarked", [], True), topology


async def test_mutual_exclusion_across_all_rows(tmp_path: Path) -> None:
    """click_bookmark never issues a removeBookmark click; click_remove_bookmark
    never issues a bookmark click — across every state/topology combination."""
    for state in ("not_bookmarked", "bookmarked"):
        for topology in ("coexist", "swap"):
            for method, forbidden in (("click_bookmark", RBM),
                                      ("click_remove_bookmark", BM)):
                page = BookmarkPage(state=state, topology=topology)
                await getattr(_broker(tmp_path, page), method)(URL)
                assert forbidden not in page.clicks, (state, topology, method)
