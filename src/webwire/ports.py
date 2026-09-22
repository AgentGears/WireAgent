"""Write-port protocols — narrow action-scoped interfaces (ChatGPT's refinement).

Each write capability depends on only the port it needs. The WriteKernel owns
the concrete WriteBroker and exposes only the required port. This prevents the
future failure mode where every write capability can accidentally see every
mutation method.

Pattern:
  BookmarkWritePort: click_bookmark(), read_bookmark_state()
  LikeWritePort: click_like(), read_like_state()
  (future: PostWritePort, FollowWritePort, etc.)

The concrete WriteBroker implements all ports; the kernel adapts it to the
narrow port each capability requests via the capability's `write_port_cls`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from webwire.envelope import ActionResult


@runtime_checkable
class BookmarkWritePort(Protocol):
    """Narrow port for bookmark mutations."""
    async def click_bookmark(self, post_url: str) -> ActionResult: ...
    async def read_bookmark_state(self, post_url: str) -> ActionResult: ...


@runtime_checkable
class LikeWritePort(Protocol):
    """Narrow port for like mutations."""
    async def click_like(self, post_url: str) -> ActionResult: ...
    async def click_unlike(self, post_url: str) -> ActionResult: ...
    async def read_like_state(self, post_url: str) -> ActionResult: ...


@runtime_checkable
class PostWritePort(Protocol):
    """Narrow port for posting text (Phase 4b)."""
    async def fill_composer(self, text: str) -> ActionResult: ...
    async def read_composer_text(self) -> ActionResult: ...
    async def click_submit(self) -> ActionResult: ...
    async def capture_posted_url(self) -> ActionResult: ...


__all__ = ["BookmarkWritePort", "LikeWritePort", "PostWritePort"]
