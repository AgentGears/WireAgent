"""Download broker — separate local-output boundary for media downloads (M2).

ChatGPT's directive: 'Do not add download() to the existing read-only broker.
Introduce a separate local-output boundary.'

This broker resolves image URLs from X posts, downloads them to local files,
and verifies the download. It's NOT part of ReadOnlyBroker (which has no side
effects) — downloading writes to the local filesystem, which is a side effect.

Safety:
- Downloads go to a configured download directory (not arbitrary paths).
- Filename is derived from the X media ID (no path traversal).
- Download is verified by checking file size > 0.
- The journal records what was downloaded.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.kill_switch import KillSwitch

logger = logging.getLogger(__name__)

__all__ = ["DownloadBroker"]


class DownloadBroker:
    """Separate local-output boundary for downloading media from X.

    NOT part of ReadOnlyBroker (which has no side effects). This broker writes
    files to the local filesystem — a deliberate, separate surface.
    """

    def __init__(
        self,
        sb: Any,
        kill_switch: KillSwitch,
        download_dir: Path,
    ) -> None:
        self._sb = sb
        self._kill = kill_switch
        self._download_dir = download_dir

    def _guard(self):
        return self._kill.guard()

    async def resolve_post_image_url(self, post_url: str) -> ActionResult:
        """Navigate to a post and extract the first tweet image URL from the DOM.

        Returns data with {image_url, image_id, alt_text}.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)

        try:
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            # Find the tweet photo — X uses pbs.twimg.com/media/ for tweet images.
            # Exclude profile avatars (which use /profile_images/).
            expr = (
                '(function(){'
                'var imgs=document.querySelectorAll("img");'
                'for(var i=0;i<imgs.length;i++){'
                'var src=imgs[i].src||"";'
                'if(src.indexOf("pbs.twimg.com/media/")>=0){'
                'return JSON.stringify({'
                'url:src,'
                'alt:imgs[i].alt||"",'
                'width:imgs[i].naturalWidth||0'
                '});}}'
                'return JSON.stringify(null);'
                '})()'
            )
            import json
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                raw = result.data.get("result", {}).get("value")
                if raw:
                    data = json.loads(raw)
                    if data:
                        # Extract the media ID from the URL for naming.
                        m = re.search(r"/media/(\w+)", data["url"])
                        media_id = m.group(1) if m else "unknown"
                        data["image_id"] = media_id
                        return ok_result(data=data)
            return soft_failure(
                "No tweet image found in the post.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"resolve_post_image_url error: {exc!r}")

    async def download_image(self, image_url: str, image_id: str) -> ActionResult:
        """Download an image from a URL to the local download directory.

        X's image CDN (pbs.twimg.com) serves images via direct HTTP. We use
        a simple HTTP fetch (urllib) rather than Super-Browser's download(),
        which has a compatibility issue with the Patchright backend's
        expect_download context manager.
        """
        if (r := self._guard()) is not None:
            return r

        # Upgrade to original quality if possible.
        download_url = image_url
        if "pbs.twimg.com/media/" in image_url:
            download_url = re.sub(r"name=\w+", "name=orig", image_url)
            if "format=" not in download_url:
                download_url = download_url.split("?")[0] + "?format=png&name=orig"

        # Determine output path from the media ID.
        ext = "png" if "format=png" in download_url else "jpg"
        filename = f"{image_id}.{ext}"
        output_path = self._download_dir / filename

        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)

            # Fetch via HTTP — X's CDN doesn't require auth for public images.
            import urllib.request
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            def _fetch() -> bytes:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()

            import asyncio
            data = await asyncio.to_thread(_fetch)

            if not data:
                return soft_failure(
                    "Download returned empty data.",
                    failure_category=FailureCategory.UNKNOWN,
                )

            output_path.write_bytes(data)
            file_size = output_path.stat().st_size

            return ok_result(data={
                "downloaded": True,
                "path": str(output_path),
                "filename": filename,
                "size_bytes": file_size,
                "source_url": download_url,
                "image_id": image_id,
            })
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"download_image error: {exc!r}")
