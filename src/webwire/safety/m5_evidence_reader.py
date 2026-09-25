"""Read-only M5 evidence surface coordinated with the browser write lease.

Layer 5 must not let verification navigate the browser concurrently with an M5
writer that owns transient DOM provenance. This adapter reuses the exact
``M5LeasedWriteBroker`` state while exposing evidence operations only; it has no
mutation methods and never hands capability code the underlying browser facade.

Two lease modes are intentional:
- pre-submit baseline capture runs under the *owned content* lease because the
  approved composer is still open;
- post-submit capture/verification runs as a one-shot read after successful
  permit consumption releases the content owner.
"""

from __future__ import annotations

import asyncio
import json

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.media_verify import verify_post_text
from webwire.safety.post_submit import capture_new_post_id
from webwire.safety.text_normalize import normalize_text

__all__ = ["M5LeasedEvidenceReader"]


class M5LeasedEvidenceReader:
    """Lease-coordinated evidence for migrated M5 effects."""

    __slots__ = ("__broker",)

    def __init__(self, broker: M5LeasedWriteBroker) -> None:
        if not isinstance(broker, M5LeasedWriteBroker):
            raise TypeError("M5 evidence reader requires M5LeasedWriteBroker")
        self.__broker = broker

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        broker = self.__broker
        return await broker._one_shot(
            lambda: M5WriteBroker.read_bookmark_state(broker, post_url)
        )

    async def read_like_state(self, post_url: str) -> ActionResult:
        broker = self.__broker
        return await broker._one_shot(
            lambda: M5WriteBroker.read_like_state(broker, post_url)
        )

    async def capture_pre_submit_ids(self) -> ActionResult:
        """Capture visible status IDs without relinquishing composer ownership.

        Unlike the historical helper, this path does not coerce CDP/evaluation
        failure to an empty set: an empty baseline can be legitimate, so error
        and emptiness must remain distinct safety facts.
        """
        broker = self.__broker

        async def operation() -> ActionResult:
            expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                'var ids={};'
                'for(var i=0;i<links.length;i++){'
                'var href=links[i].getAttribute("href")||"";'
                'var m=href.match(/\\/status\\/(\\d+)/);'
                'if(m)ids[m[1]]=1;'
                '}'
                'return JSON.stringify(Object.keys(ids));'
                '})()'
            )
            try:
                result = await broker._sb._controller._cdp.evaluate(expr)
            except Exception as exc:  # noqa: BLE001
                return soft_failure(
                    f"pre-submit identity baseline evaluation failed: {exc!r}",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not result.ok or not result.data or "exceptionDetails" in result.data:
                return soft_failure(
                    "pre-submit identity baseline evaluation did not return evidence",
                    failure_category=FailureCategory.UNKNOWN,
                )
            raw = result.data.get("result", {}).get("value")
            if not isinstance(raw, str):
                return soft_failure(
                    "pre-submit identity baseline returned malformed evidence",
                    failure_category=FailureCategory.UNKNOWN,
                )
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                return soft_failure(
                    f"pre-submit identity baseline was not valid JSON: {exc!r}",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not isinstance(parsed, list) or not all(
                isinstance(value, str) for value in parsed
            ):
                return soft_failure(
                    "pre-submit identity baseline had invalid status IDs",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"status_ids": sorted(set(parsed))})

        return await broker._owned_content(operation)

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        """Find a post that appeared after the approved submit boundary."""
        broker = self.__broker

        async def operation() -> ActionResult:
            post_id, post_url = await capture_new_post_id(
                broker,
                set(pre_submit_ids),
                exclude_ids=set(exclude_ids or ()),
            )
            if not post_id or not post_url:
                return soft_failure(
                    "post-submit evidence did not identify a new status",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"post_id": post_id, "post_url": post_url})

        return await broker._one_shot(operation)

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        """Verify the exact posted status text under the shared browser lease."""
        broker = self.__broker

        async def operation() -> ActionResult:
            matched = await verify_post_text(broker, post_url, normalized_text)
            if not matched:
                return soft_failure(
                    "post-submit text evidence did not match the approved text",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(
                data={
                    "text_matches": True,
                    "post_url": post_url,
                }
            )

        return await broker._one_shot(operation)

    async def verify_reply_in_thread(
        self,
        *,
        target_post_id: str,
        reply_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult:
        """Prove an exact captured reply belongs to the approved target thread.

        Confirmation requires one unique target article and one unique reply
        article on the target conversation page. Each article must own its own
        timestamp link directly (nested quote timestamps are ignored), the reply
        must render after the target, its direct timestamp path must identify the
        approved actor, and its direct tweet text must match the approved text.
        """
        broker = self.__broker
        target_url = f"https://x.com/i/status/{target_post_id}"
        approved_actor = actor_id.lstrip("@").casefold()
        if not approved_actor:
            return soft_failure(
                "reply verification requires an approved actor identity",
                failure_category=FailureCategory.SECURITY,
            )

        async def operation() -> ActionResult:
            nav = await broker._sb.navigate(target_url, wait_until="domcontentloaded")
            if not nav.ok:
                return nav

            expr = (
                "(function(){"
                f"var target={json.dumps(target_post_id)},reply={json.dumps(reply_post_id)};"
                "function ownStatus(art){"
                "var links=art.querySelectorAll('a[href]'),found=[];"
                "for(var i=0;i<links.length;i++){var a=links[i];"
                "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
                "try{var u=new URL(a.href,location.href);"
                "var m=u.pathname.match(/^\\/([^/]+)\\/status\\/(\\d+)(?:\\/|$)/);"
                "if(m)found.push({actor:m[1],id:m[2],path:u.pathname});}catch(e){}"
                "}"
                "var uniq={};for(var j=0;j<found.length;j++)uniq[found[j].id]=found[j];"
                "var ids=Object.keys(uniq);return ids.length===1?uniq[ids[0]]:null;"
                "}"
                "function directText(art){var ts=art.querySelectorAll(\"[data-testid='tweetText']\");"
                "var found=[];for(var i=0;i<ts.length;i++)"
                "if(ts[i].closest('article')===art)found.push(ts[i]);"
                "return found.length===1?(found[0].innerText||''):null;}"
                "var arts=document.querySelectorAll('article'),targets=[],replies=[];"
                "for(var ai=0;ai<arts.length;ai++){var own=ownStatus(arts[ai]);if(!own)continue;"
                "if(own.id===target)targets.push({i:ai,own:own});"
                "if(own.id===reply)replies.push({i:ai,own:own,text:directText(arts[ai])});}"
                "if(targets.length!==1||replies.length!==1)"
                "return JSON.stringify({status:'missing_or_ambiguous',targets:targets.length,replies:replies.length});"
                "var t=targets[0],r=replies[0];"
                "return JSON.stringify({status:'found',targetIndex:t.i,replyIndex:r.i,"
                "replyActor:r.own.actor,replyPath:r.own.path,replyText:r.text});})()"
            )

            last: dict[str, object] | None = None
            for _ in range(20):
                try:
                    evaluated = await broker._sb._controller._cdp.evaluate(expr)
                except Exception as exc:  # noqa: BLE001
                    return soft_failure(
                        f"reply thread evidence evaluation failed: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not evaluated.ok or not evaluated.data or "exceptionDetails" in evaluated.data:
                    return soft_failure(
                        "reply thread evidence evaluation did not return evidence",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                raw = evaluated.data.get("result", {}).get("value")
                if not isinstance(raw, str):
                    return soft_failure(
                        "reply thread evidence returned malformed evidence",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    return soft_failure(
                        f"reply thread evidence was not valid JSON: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not isinstance(parsed, dict):
                    return soft_failure(
                        "reply thread evidence was not an object",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                last = parsed
                if parsed.get("status") == "found":
                    break
                await asyncio.sleep(0.25)

            if last is None or last.get("status") != "found":
                return soft_failure(
                    "reply status was not uniquely visible on the approved target thread",
                    failure_category=FailureCategory.UNKNOWN,
                )
            target_index = last.get("targetIndex")
            reply_index = last.get("replyIndex")
            reply_actor = last.get("replyActor")
            reply_text = last.get("replyText")
            reply_path = last.get("replyPath")
            if (
                not isinstance(target_index, int)
                or not isinstance(reply_index, int)
                or reply_index <= target_index
            ):
                return soft_failure(
                    "captured reply was not rendered downstream of the approved target",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not isinstance(reply_actor, str) or reply_actor.casefold() != approved_actor:
                return soft_failure(
                    "captured reply actor did not match the approved actor",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not isinstance(reply_text, str) or normalize_text(reply_text) != normalize_text(
                normalized_text
            ):
                return soft_failure(
                    "captured reply text did not match the approved text",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not isinstance(reply_path, str) or f"/status/{reply_post_id}" not in reply_path:
                return soft_failure(
                    "captured reply timestamp did not own the expected status ID",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(
                data={
                    "thread_bound": True,
                    "target_post_id": target_post_id,
                    "reply_post_id": reply_post_id,
                    "reply_actor": reply_actor,
                    "reply_url": f"https://x.com{reply_path}",
                    "text_matches": True,
                }
            )

        return await broker._one_shot(operation)
