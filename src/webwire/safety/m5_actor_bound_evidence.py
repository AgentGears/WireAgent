"""Actor-bound standalone-post evidence for Layer 5.

The historical post-submit helpers accepted the first newly visible status link
and the historical text verifier could select an article through any descendant
status link. Neither is sufficient for M5 terminal truth: hydration gaps,
nested statuses, or concurrent same-text posts must never be promoted to
``EFFECT_CONFIRMED``.

The supported live reader therefore establishes a stable pre-submit snapshot of
direct article-owned status IDs, requires one stable unique new direct status
after the submit boundary, and verifies that status through its direct timestamp,
actor, canonical URL, and direct text. The executor finally compares the
observed actor with the immutable approved actor carried by the consumed permit.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.m5_evidence_reader import M5LeasedEvidenceReader
from webwire.safety.text_normalize import normalize_text

__all__ = ["M5ActorBoundEvidenceReader", "status_url_identity"]

_STATUS_PATH = re.compile(r"^/([^/]+)/status/(\d+)(?:/|$)")
_DIRECT_STATUS_SNAPSHOT_JS = (
    "(function(){"
    "function ownStatus(art){"
    "var links=art.querySelectorAll('a[href]'),found=[];"
    "for(var i=0;i<links.length;i++){var a=links[i];"
    "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
    "try{var u=new URL(a.href,location.href);"
    "var m=u.pathname.match(/^\\/([^/]+)\\/status\\/(\\d+)(?:\\/|$)/);"
    "if(m)found.push({actor:m[1],id:m[2],path:u.pathname});}catch(e){}"
    "}"
    "var uniq={};for(var j=0;j<found.length;j++)uniq[found[j].id]=found[j];"
    "var ids=Object.keys(uniq);return ids.length===1?uniq[ids[0]]:null;}"
    "var arts=document.querySelectorAll('article'),out=[],seen={};"
    "for(var ai=0;ai<arts.length;ai++){var own=ownStatus(arts[ai]);"
    "if(own&&!seen[own.id]){seen[own.id]=1;out.push(own);}}"
    "return JSON.stringify(out);})()"
)


def status_url_identity(url: str) -> tuple[str, str] | None:
    """Return normalized ``(actor, status_id)`` for one canonical X permalink."""

    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"x.com", "www.x.com"}:
        return None
    match = _STATUS_PATH.match(parsed.path)
    if match is None:
        return None
    actor, post_id = match.groups()
    if actor.casefold() in {"i", "intent"}:
        return None
    return actor.lstrip("@").casefold(), post_id


class M5ActorBoundEvidenceReader(M5LeasedEvidenceReader):
    """M5 content evidence with direct actor/status ownership for plain posts."""

    __slots__ = ("__strong_broker",)

    def __init__(self, broker: M5LeasedWriteBroker) -> None:
        super().__init__(broker)
        self.__strong_broker = broker

    async def _direct_status_snapshot(self) -> list[dict[str, str]] | None:
        broker = self.__strong_broker
        try:
            evaluated = await broker._sb._controller._cdp.evaluate(
                _DIRECT_STATUS_SNAPSHOT_JS
            )
        except Exception:  # noqa: BLE001
            return None
        if not evaluated.ok or not evaluated.data or "exceptionDetails" in evaluated.data:
            return None
        raw = evaluated.data.get("result", {}).get("value")
        if not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, list):
            return None
        statuses: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                return None
            actor = item.get("actor")
            post_id = item.get("id")
            path = item.get("path")
            if (
                not isinstance(actor, str)
                or not isinstance(post_id, str)
                or not post_id.isdigit()
                or not isinstance(path, str)
            ):
                return None
            statuses.append({"actor": actor, "id": post_id, "path": path})
        return statuses

    async def capture_pre_submit_ids(self) -> ActionResult:
        """Require two consecutive equal direct-status snapshots before submit."""

        broker = self.__strong_broker

        async def operation() -> ActionResult:
            previous: tuple[str, ...] | None = None
            for _ in range(20):
                statuses = await self._direct_status_snapshot()
                if statuses is None:
                    return soft_failure(
                        "pre-submit direct-status baseline evaluation failed",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                current = tuple(sorted({item["id"] for item in statuses}))
                if previous is not None and current == previous:
                    return ok_result(data={"status_ids": list(current)})
                previous = current
                await asyncio.sleep(0.25)
            return soft_failure(
                "pre-submit direct-status baseline did not stabilize",
                failure_category=FailureCategory.UNKNOWN,
            )

        return await broker._owned_content(operation)

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        """Require one stable unique new direct article-owned status after submit."""

        broker = self.__strong_broker
        excluded = set(pre_submit_ids) | set(exclude_ids or ())

        async def operation() -> ActionResult:
            previous_candidate: tuple[str, str, str] | None = None
            for _ in range(20):
                statuses = await self._direct_status_snapshot()
                if statuses is None:
                    return soft_failure(
                        "post-submit direct-status capture evaluation failed",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                candidates = [item for item in statuses if item["id"] not in excluded]
                if len(candidates) > 1:
                    return soft_failure(
                        "post-submit status identity is ambiguous: multiple new direct statuses",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if len(candidates) == 1:
                    candidate = candidates[0]
                    current = (
                        candidate["actor"].lstrip("@").casefold(),
                        candidate["id"],
                        candidate["path"],
                    )
                    post_url = f"https://x.com{candidate['path']}"
                    identity = status_url_identity(post_url)
                    expected = (current[0], current[1])
                    if identity != expected:
                        return soft_failure(
                            "post-submit direct timestamp produced a non-canonical status URL",
                            failure_category=FailureCategory.UNKNOWN,
                        )
                    if previous_candidate == current:
                        return ok_result(
                            data={
                                "post_id": candidate["id"],
                                "post_url": post_url,
                                "post_actor": candidate["actor"],
                                "direct_status_owned": True,
                            }
                        )
                    previous_candidate = current
                else:
                    previous_candidate = None
                await asyncio.sleep(0.25)
            return soft_failure(
                "post-submit evidence did not stabilize on one unique new direct status",
                failure_category=FailureCategory.UNKNOWN,
            )

        return await broker._one_shot(operation)

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        identity = status_url_identity(post_url)
        if identity is None:
            return soft_failure(
                "post verification requires an actor-owned canonical status URL",
                failure_category=FailureCategory.UNKNOWN,
            )
        url_actor, post_id = identity
        broker = self.__strong_broker

        async def operation() -> ActionResult:
            nav = await broker._sb.navigate(post_url, wait_until="domcontentloaded")
            if not nav.ok:
                return soft_failure(
                    "post verification navigation failed",
                    failure_category=FailureCategory.UNKNOWN,
                )

            expr = (
                "(function(){"
                f"var target={json.dumps(post_id)};"
                "function ownStatus(art){"
                "var links=art.querySelectorAll('a[href]'),found=[];"
                "for(var i=0;i<links.length;i++){var a=links[i];"
                "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
                "try{var u=new URL(a.href,location.href);"
                "var m=u.pathname.match(/^\\/([^/]+)\\/status\\/(\\d+)(?:\\/|$)/);"
                "if(m)found.push({actor:m[1],id:m[2],path:u.pathname});}catch(e){}"
                "}"
                "var uniq={};for(var j=0;j<found.length;j++)uniq[found[j].id]=found[j];"
                "var ids=Object.keys(uniq);return ids.length===1?uniq[ids[0]]:null;}"
                "function directText(art){"
                "var nodes=art.querySelectorAll(\"[data-testid='tweetText']\"),found=[];"
                "for(var i=0;i<nodes.length;i++)"
                "if(nodes[i].closest('article')===art)found.push(nodes[i]);"
                "if(found.length===0)return '';"
                "return found.length===1?(found[0].innerText||''):null;}"
                "var arts=document.querySelectorAll('article'),matches=[];"
                "for(var ai=0;ai<arts.length;ai++){var own=ownStatus(arts[ai]);"
                "if(own&&own.id===target)matches.push({own:own,text:directText(arts[ai])});}"
                "if(matches.length!==1)return JSON.stringify({status:'missing_or_ambiguous',"
                "matches:matches.length});"
                "var m=matches[0];return JSON.stringify({status:'found',actor:m.own.actor,"
                "id:m.own.id,path:m.own.path,text:m.text});})()"
            )

            last: dict[str, object] | None = None
            for _ in range(20):
                try:
                    evaluated = await broker._sb._controller._cdp.evaluate(expr)
                except Exception as exc:  # noqa: BLE001
                    return soft_failure(
                        f"post direct-ownership evidence evaluation failed: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not evaluated.ok or not evaluated.data or "exceptionDetails" in evaluated.data:
                    return soft_failure(
                        "post direct-ownership evidence did not return evidence",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                raw = evaluated.data.get("result", {}).get("value")
                if not isinstance(raw, str):
                    return soft_failure(
                        "post direct-ownership evidence was malformed",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    return soft_failure(
                        f"post direct-ownership evidence was not valid JSON: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not isinstance(parsed, dict):
                    return soft_failure(
                        "post direct-ownership evidence was not an object",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                last = parsed
                if parsed.get("status") == "found":
                    break
                await asyncio.sleep(0.25)

            if last is None or last.get("status") != "found":
                return soft_failure(
                    "captured status was not uniquely owned by one direct article",
                    failure_category=FailureCategory.UNKNOWN,
                )
            actor = last.get("actor")
            observed_id = last.get("id")
            path = last.get("path")
            text = last.get("text")
            if (
                not isinstance(actor, str)
                or not isinstance(observed_id, str)
                or observed_id != post_id
                or not isinstance(path, str)
                or not isinstance(text, str)
            ):
                return soft_failure(
                    "captured status ownership evidence was incomplete",
                    failure_category=FailureCategory.UNKNOWN,
                )
            direct_url = f"https://x.com{path}"
            direct_identity = status_url_identity(direct_url)
            if direct_identity != (actor.lstrip("@").casefold(), post_id):
                return soft_failure(
                    "captured direct timestamp did not own the expected status URL",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if actor.lstrip("@").casefold() != url_actor:
                return soft_failure(
                    "captured status actor disagreed with the captured status URL",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if normalize_text(text) != normalize_text(normalized_text):
                return soft_failure(
                    "captured direct post text did not match the approved text",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(
                data={
                    "text_matches": True,
                    "direct_status_owned": True,
                    "post_id": post_id,
                    "post_actor": actor,
                    "post_url": direct_url,
                }
            )

        return await broker._one_shot(operation)
