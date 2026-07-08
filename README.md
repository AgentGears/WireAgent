# Agent-WebWire

Browser-native X/Twitter capability layer for AI agents — built on the user's
own [Super-Browser](https://github.com/Octo-Lex/Super-Browser) SDK.

**Phase 0a (current):** session + identity (`whoami`) + `health` + envelope
(reused Super-Browser `ActionResult`) + append-only journal + kill switch +
read-only browser-action boundary. **No write capabilities exist** — they are
physically absent, not merely disabled.

## Framing

> "AI proposes, human approves, system enforces budgets, browser executes,
> journal proves." Not "anti-bot bypass."

Personal, single-user, local-only. Not a product, not multi-tenant.

## Install (editable, local)

```bash
# Super-Browser must be installed first (the SDK dependency).
pip install -e C:/Next-Era/Super-Browser
pip install -e ".[dev]"
```

## Phase 0a usage

```python
import asyncio
from webwire import Dispatcher, WebWireConfig

async def main():
    async with dispatcher_starter() as d:  # see below
        who = await d.invoke("whoami")
        print(who.data)
        h = await d.invoke("health")
        print(h.data)

asyncio.run(main())
```

`Dispatcher` owns the lifecycle:

```python
d = Dispatcher()
await d.start()          # attaches to logged-in Chrome via Super-Browser
await d.invoke("whoami")
await d.invoke("health")
await d.stop()
```

### Kill switch

Trip it (refuses all further capability execution; leaves the browser intact):

```python
d.kill_switch.trip()     # in-process flag + .webwire/kill hot file
```

Or externally: create `.webwire/kill`. Reset: remove the file and call
`d.kill_switch.reset()`.

## Safety boundary (Phase 0a)

- Capability code never receives the raw `SuperBrowser` facade — only the
  `ReadOnlyBroker`.
- The broker exposes only: `navigate` (X-surface constrained), `reload`,
  `go_back`, `go_forward`, `observe`, `extract`, `list_tabs`, `switch_tab`.
- Mutating primitives (`click`, `fill`, `act`, `delegate`, `upload_file`,
  `download`, `check`, `uncheck`, `type_text`) are unreachable.
- Kill switch is checked at the dispatcher top AND every broker method entry.
- Journal writes one NDJSON record per capability invocation at
  `.webwire/journal.ndjson`; screenshots default to failure-only.

## Status

Pre-Alpha. Phase 0a substrate. Phase 0b (write-safety kernel) and Phase 1
(golden read) are next.
