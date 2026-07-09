# Agent-WebWire — Living Project State

> Single source of truth for project state, consulted at the start of every
> ChatGPT engagement to defeat conversation fragmentation. Update on every
> meaningful change. Append-only history at the bottom.

## Identity

- **Repo:** `C:\Next-Era\Agent-WebWire` (local, git-initialized)
- **Bound ChatGPT project:** `g-p-6a46471ac4308191bedb18f9a3562c95` ("Agent-WebWire")
- **Active conversation:** see `.chatgpt-collaboration.json` (binding file)
- **What it is:** Personal, single-user, local-only, browser-native X/Twitter
  capability layer for AI agents. Built on the user's own Super-Browser SDK.
  Not a product, not multi-tenant.
- **Framing:** "AI proposes, human approves, system enforces budgets, browser
  executes, journal proves." NOT "anti-bot bypass."

## Architecture invariants (do not violate)

1. **Agent-WebWire owns its browser by default.** Launch (owned) is the golden
   path. Attach mode is a non-production diagnostic path, refused unless
   `allow_attach=True`. Never attach to a foreign browser (e.g. the
   ChatGPT-Web2API bridge's Chrome). [Review Q4, commit 9441617+]
2. **Capability contracts, not commands.** Capabilities receive only the
   `ReadOnlyBroker` — never the raw `SuperBrowser` facade.
3. **Write capabilities are physically absent in Phase 0a.** Registry tier-gates
   on READ; WRITE registration raises `PermissionError`. Not "disabled" — absent.
4. **Kill switch is checked at dispatcher top AND every broker method entry.**
   Kill dominates unsupported resolution: when killed, EVERY invocation returns
   `killed`. Kill ≠ `sb.stop()` (leaves browser intact for debugging).
5. **Persistence is a convenience, not an authority source.** `load_session`
   success means only "cookies loaded." `whoami` is the real auth gate. Never
   save a logged-out jar over a known-good file. [Review Q1-Q3]
6. **Envelope reuse, not rebuild.** Reuse Super-Browser's `ActionResult` + the
   `FailureCategory` taxonomy; extend only at the registry boundary.

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| **0a** | session + whoami + health + envelope + journal + kill switch + read-only broker | **IMPLEMENTED + LIVE-VERIFIED** (60 tests pass; live smoke all 6 gates PASS, identity @infaag resolved) |
| **0b** | write-safety kernel (token bucket, dedupe, risk registry, dry-run, compose→…→verify) | **IMPLEMENTED** (114 tests pass; write pipeline + token-bound confirmation + 4-tier risk + global circuit breaker) |
| **1** | golden read (`read <post_url>`) | **IMPLEMENTED + LIVE-VERIFIED** (read resolves real post: id/handle/created_at/text/metrics) |
| **2** | read fan-out (`read_profile`) | **IMPLEMENTED + LIVE-VERIFIED** (read_profile @jack: 10 posts, retweet provenance correct) |
| **3** | reversible writes (bookmark/like) | blocked on 0b |
| **4** | public writes | blocked on Phase 3 |
| **5** | analytics (separate adapter family) | blocked on Phase 4 |

## Current status

**Phase 0a-live PASSED — all 6 gates satisfied** (live, against real X, identity
`@infaag` resolved via Profile-link href). The control loop is proven end-to-end:
owned browser → session restore → whoami gate → kill invariant → journal proof.

Resolved along the way:
- Session persistence via cookie serialization (save_session/load_session).
- whoami primary path = `broker.query_attr` reading the Profile nav link href
  (`data-testid=AppTabBar_Profile_Link` → `/<handle>`). AX-parse is fallback.
- Hydration wait (4s) in whoami — X is a React SPA; observe() before hydration
  returns an empty snapshot.
- Strategy B tightened: rejects single-char names and brand names ("X", "Grok").

## Session persistence (key architectural finding)

- Super-Browser's `PATCHRIGHT_LAUNCH` calls `chromium.launch()` (ephemeral
  profile, discarded on close). `SessionConfig.user_data_dir` is a dead field.
- `launch_persistent_context` (the fix) is Playwright-only; Super-Browser
  supports Patchright/Playwright/Selenium/CDP, so they deliberately built
  **cookie serialization** instead: `save_session`/`load_session` via
  StealthBridge (backend-agnostic). [commit da38412, BATCH-52]
- `SessionConfig.session_file` claims auto-save/load but is also unimplemented;
  only manual `save_session(path)`/`load_session(path)` work.
- Agent-WebWire wires these: load on start, eager checkpoint after verified
  whoami, atomic save, attach-gate, ownership diagnostic. **No Super-Browser
  changes.**
- Cookie-only restore is fragile (no localStorage/IndexedDB). Expect re-login
  on token expiry. True profile persistence is a future Super-Browser feature.

## Transport

- **Browser-first** (Super-Browser provides the real, logged-in Chromium).
- HTTP fast-path deferred; if added, must be a transport behind the same
  capability contract, never an independent state source (split-brain avoidance).

## Safety model (for Phase 0b/3)

Every write: compose → preview → policy → confirm → execute → journal → verify.
Compensation is per-action metadata (`supports_compensation` /
`compensation_action` / `residual_side_effects`), NOT a universal undo stage.
Per-action token bucket + per-target dedupe key. Kill switch before every write.

## Module map (src/webwire/)

| File | Role |
|------|------|
| `config.py` | `WebWireConfig` (immutable): state_dir, screenshot policy, kill_file, allowed_url_prefixes, allow_attach, session_file |
| `session.py` | `SessionManager` — owns SuperBrowser lifecycle; load/checkpoint session; ownership mode; attach gate |
| `envelope.py` | reuses `ActionResult`; 6 registry-boundary failure constructors |
| `safety/kill_switch.py` | flag + hot file + boot env; guard() at dispatcher + broker |
| `journal.py` | append-only NDJSON; per-invocation; failure-only screenshots default |
| `broker.py` | `ReadOnlyBroker` — only cap surface; origin-based URL guard; kill guard at entry |
| `capabilities/whoami.py` | observe → parse → extract fallback; `identity_unresolved_on_authenticated_surface` on failure |
| `capabilities/health.py` | probe-set diagnostic (not hard gate); reports browser_ownership |
| `registry.py` | tier-gated; WRITE → PermissionError |
| `dispatcher.py` | wires all; checkpoint on whoami success; kill precedence |

## Recent commits

- `9441617` — Review iteration: kill precedence, whoami failure code, origin URL guard
- `0d094f1` — Phase 0a: session + identity + health + envelope + journal + kill switch + read-only broker
- (uncommitted) — cookie-persistence wiring (load/checkpoint/ownership/attach-gate)

## Verification snapshot

- 60 unit tests pass (0.66s). Coverage ~71%.
- **Live smoke: ALL 6 GATES PASS** (real X, identity @infaag).

## Open items / known gaps

- [x] ~~Complete one-time X login → session.json~~ (done; 22 cookies incl. auth_token)
- [x] ~~Re-run live smoke; pass Gate 3; declare 0a-live done~~ (done)
- [ ] Screenshot capture plumbed but unimplemented (broker.diagnostic_screenshot())
- [ ] No CLI yet; docs/ now has STATE.md only
- [ ] `identity_resolved` as a health probe (ChatGPT Q4 note, optional)
- [ ] Future: persistent-context feature in Super-Browser (launch_persistent_context behind a flag)
- [ ] Cookie-only persistence is fragile — expect re-login on token expiry

## History

- 2026-07-08 (late): Phase 0a-LIVE PASSED. All 6 gates, identity @infaag resolved via Profile-link href. Session persistence via cookie serialization working end-to-end.
- 2026-07-08: Phase 0a implemented + review-hardened + cookie-persistence wired.
