"""Strict lease-coordinated evidence for M5 target deletion.

Deletion confirmation is intentionally stronger than the legacy verifier. A
missing article, failed navigation, generic error page, or unloaded timeline is
not proof that the approved target was deleted. ``deleted`` is returned only
when the browser remains on the approved target permalink, the target article
is absent, and exactly one standalone deletion tombstone is visible outside any
article. Everything else is ``unknown``.
"""

from __future__ import annotations

import asyncio
import json

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker

__all__ = ["M5LeasedDeleteEvidenceReader"]


class M5LeasedDeleteEvidenceReader:
    """Read only target-delete evidence under the shared M5 browser lease."""

    __slots__ = ("__broker",)

    def __init__(self, broker: M5LeasedWriteBroker) -> None:
        if not isinstance(broker, M5LeasedWriteBroker):
            raise TypeError("M5 delete evidence requires M5LeasedWriteBroker")
        self.__broker = broker

    async def read_delete_state(self, post_url: str, post_id: str) -> ActionResult:
        if not isinstance(post_id, str) or not post_id.isdigit():
            return soft_failure(
                "delete evidence requires a numeric target post id",
                failure_category=FailureCategory.SECURITY,
            )

        broker = self.__broker

        async def operation() -> ActionResult:
            nav = await broker._sb.navigate(post_url, wait_until="domcontentloaded")
            if not nav.ok:
                return soft_failure(
                    "delete evidence navigation failed",
                    failure_category=FailureCategory.UNKNOWN,
                )

            expr = (
                "(function(){"
                f"var target={json.dumps(post_id)};"
                "var path=location.pathname||'';"
                "var targetSuffix='/status/'+target;"
                "var atTarget=path===targetSuffix||path.endsWith(targetSuffix)||"
                "path.indexOf(targetSuffix+'/')>=0;"
                "if(!atTarget)return JSON.stringify({status:'wrong_page',path:path});"
                "function ownStatus(art){"
                "var links=art.querySelectorAll('a[href]'),ids={};"
                "for(var i=0;i<links.length;i++){var a=links[i];"
                "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
                "try{var u=new URL(a.href,location.href);"
                "var m=u.pathname.match(/\\/status\\/(\\d+)(?:\\/|$)/);"
                "if(m)ids[m[1]]=1;}catch(e){}}"
                "var keys=Object.keys(ids);return keys.length===1?keys[0]:null;}"
                "var arts=document.querySelectorAll('article'),matches=0;"
                "for(var ai=0;ai<arts.length;ai++){if(ownStatus(arts[ai])===target)matches++;}"
                "if(matches===1)return JSON.stringify({status:'present'});"
                "if(matches>1)return JSON.stringify({status:'ambiguous',matches:matches});"
                "var phrases={'This post was deleted':1,'Post deleted':1,"
                "'This Post was deleted by its author.':1,"
                "'This post was deleted by its author.':1};"
                "var nodes=document.querySelectorAll('span,div'),stones=[];"
                "for(var ni=0;ni<nodes.length;ni++){var n=nodes[ni];"
                "if(n.closest('article'))continue;"
                "var t=(n.innerText||'').trim();if(phrases[t])stones.push(t);}"
                "var uniq={};for(var si=0;si<stones.length;si++)uniq[stones[si]]=1;"
                "var ts=Object.keys(uniq);"
                "if(ts.length===1)return JSON.stringify({status:'deleted',tombstone:ts[0]});"
                "if(ts.length>1)return JSON.stringify({status:'ambiguous_tombstone',count:ts.length});"
                "return JSON.stringify({status:'pending'});})()"
            )

            last: dict[str, object] | None = None
            terminal_statuses = {
                "present",
                "deleted",
                "ambiguous",
                "ambiguous_tombstone",
                "wrong_page",
            }
            for _ in range(20):
                try:
                    evaluated = await broker._sb._controller._cdp.evaluate(expr)
                except Exception as exc:  # noqa: BLE001
                    return soft_failure(
                        f"delete evidence evaluation failed: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not evaluated.ok or not evaluated.data or "exceptionDetails" in evaluated.data:
                    return soft_failure(
                        "delete evidence evaluation did not return evidence",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                raw = evaluated.data.get("result", {}).get("value")
                if not isinstance(raw, str):
                    return soft_failure(
                        "delete evidence returned malformed evidence",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    return soft_failure(
                        f"delete evidence was not valid JSON: {exc!r}",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                if not isinstance(parsed, dict):
                    return soft_failure(
                        "delete evidence was not an object",
                        failure_category=FailureCategory.UNKNOWN,
                    )
                last = parsed
                if parsed.get("status") in terminal_statuses:
                    break
                await asyncio.sleep(0.25)

            if last is None:
                return soft_failure(
                    "delete evidence produced no state",
                    failure_category=FailureCategory.UNKNOWN,
                )
            status = last.get("status")
            if status == "present":
                return ok_result(
                    data={
                        "post_state": "present",
                        "target_post_id": post_id,
                        "evidence": "unique_direct_target_article",
                    }
                )
            if status == "deleted":
                return ok_result(
                    data={
                        "post_state": "deleted",
                        "target_post_id": post_id,
                        "evidence": "target_permalink_explicit_delete_tombstone",
                        "tombstone": last.get("tombstone"),
                    }
                )
            return soft_failure(
                "delete state could not be established from target-bound explicit evidence",
                failure_category=FailureCategory.UNKNOWN,
            )

        return await broker._one_shot(operation)
