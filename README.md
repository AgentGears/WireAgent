# Agent-WebWire

Browser-native X/Twitter capability layer for AI agents — built on the user's
own [Super-Browser](https://github.com/Octo-Lex/Super-Browser) SDK.

**Status: v0.2 (M1–M4c).** 19 capabilities — 7 read, 12 write — every
live-verified capability proven against the real site, with a write-safety
kernel that gates every mutation behind token-bound human confirmation.

## Framing

> "AI proposes, human approves, system enforces budgets, browser executes,
> journal proves." Not "anti-bot bypass."

Personal, single-user, local-only. Not a product, not multi-tenant.

## Capabilities

| Reads | Writes |
|---|---|
| `whoami` — identity via profile-link href | `bookmark_post` — private, reversible |
| `read` — single post with metrics | `like_post` — directional compensation |
| `read_profile` — fan-out post enumeration | `compose_post` — dry-run preview only |
| `read_thread` — conversation slice | `post_text` / `reply_post` / `quote_post` |
| `read_search` — top/latest/people/media tabs | `post_photo` / `reply_photo` / `quote_photo` |
| `download_image` — separate local-output broker | `post_multi_image` / `reply_multi_image` — ordered manifest transaction |
| `health` — DOM probes, hydration polling, core gating | `quote_multi_image` — quoted target + 2/2 media verified |

All multi-image writes share one media-compose harness
(`safety/media_compose.py`): preflight-all-before-any-upload, exact-count
verification per attachment, composition re-verification, abort-and-cleanup
on any partial failure, and honest verification (never claims byte
equivalence, rendered order, or unproven attachments).

## Install (editable, local)

```bash
# Super-Browser must be installed first (the SDK dependency).
pip install -e C:/Next-Era/Super-Browser
pip install -e ".[dev]"
```

## Usage — the two-phase confirmation flow

Every write requires a human-approved, intent-bound token. No capability can
mutate without it.

```python
import asyncio
from webwire import Dispatcher, WebWireConfig

async def main():
    d = Dispatcher()
    await d.start()

    who = await d.invoke("whoami")               # binds actor identity
    print(who.data)                              # {'handle': '...', ...}

    # Phase 1: preview + confirmation token (no mutation).
    p1 = await d.invoke("bookmark_post",
                        {"post_url": "https://x.com/a/status/123"})
    print(p1.data["data"]["preview"])

    # Phase 2: the approved intent, token-bound. Any change to text,
    # target, media, or risk tier since Phase 1 invalidates the token.
    p2 = await d.invoke("bookmark_post",
                        {"post_url": "https://x.com/a/status/123",
                         "confirmation_token": p1.data["data"]["confirmation_token"]})
    print(p2.data["trace"]["stages"])

    await d.stop()

asyncio.run(main())
```

### Kill switch

```python
d.kill_switch.trip()   # in-process flag + .webwire/kill hot file
```

Refuses all further capability execution at the dispatcher top AND every
broker method entry; leaves the browser intact. External trip: create
`.webwire/kill`.

## Safety model

- **Capability contracts, not commands.** Capabilities receive only the
  `ReadOnlyBroker`, `WriteBroker`, or `DownloadBroker` — never the raw
  browser facade. The write surface is semantic (`click_like`, never `click`).
- **Token-bound confirmation.** The confirmation token binds a SHA-256 of the
  full intent (text, media digests, target, risk tier). Mutation after
  approval invalidates the token.
- **Registry-gated risk.** The kernel verifies every write against the risk
  registry — unknown action types and meta drift are denied, not trusted.
- **Budgets that survive restarts.** Per-action and global rate limits and
  the dedupe store rebuild from the journal on every start.
- **Uncertain submits are treated as happened.** A submit whose outcome is
  unknown records its dedupe key and blocks same-intent retries.
- **Journal as proof.** Append-only NDJSON, rotated whole (never rewritten);
  write facts (`action_type`, `risk_tier`, `dedupe_key`) journaled per
  invocation; URLs redacted at write time.
- **Honest verification.** Results record the basis of proof
  (`identity_aware_post_submit`, `thread_readback`, `execution_path`) and
  never claim more.

## Honest limitations

Pre-alpha software driving X's real DOM: selectors can churn; verification
is bounded by what the DOM exposes (X hides per-image identity and quoted
targets). Sessions are cookie-persistence only. The journal is the audit
record, not a guarantee of side-effect freedom. See `docs/STATE.md` — the
living project state — for current capability status, known gaps, and the
invariant list (15 architecture invariants).
