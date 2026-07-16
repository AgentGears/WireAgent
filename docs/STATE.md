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

## Current version

**v0.2 (M1-M4a complete; M4b/M4c next)** — 17 capabilities, 203 tests, 28 commits.

## Architecture invariants (do not violate)

1. **Agent-WebWire owns its browser by default.** Launch (owned) is the golden
   path. Attach mode is a non-production diagnostic path, refused unless
   `allow_attach=True`. Never attach to a foreign browser (e.g. the
   ChatGPT-Web2API bridge's Chrome). [Review Q4, commit 9441617+]
2. **Capability contracts, not commands.** Capabilities receive only the
   `ReadOnlyBroker` (reads), `WriteBroker` (writes via WriteKernel), or
   `DownloadBroker` (media download) — never the raw `SuperBrowser` facade.
3. **Write capabilities are physically absent until the safety kernel allows
   them.** Registry tier-gates; WRITE registration is allowed but routing is
   through the WriteKernel pipeline.
4. **Kill switch is checked at dispatcher top AND every broker method entry.**
   Kill dominates unsupported resolution: when killed, EVERY invocation returns
   killed. Kill ≠ `sb.stop()` (leaves browser intact for debugging).
5. **Persistence is a convenience, not an authority source.** `load_session`
   success means only "cookies loaded." `whoami` is the real auth gate. Never
   save a logged-out jar over a known-good file. [Review Q1-Q3]
6. **Envelope reuse, not rebuild.** Reuse Super-Browser's `ActionResult` + the
   `FailureCategory` taxonomy; extend only at the registry boundary.
7. **Verify resolved values, not just ok=True.** Caught 5+ real bugs (whoami
   "X"-logo, bookmark-as-billions, composer read-back empty, reply URL capture,
   quote attachment). This is project law.
8. **Writes are semantic, not toggle-based.** `click_like()` ≠ `click_unlike()`.
   [ChatGPT M3b directive]
9. **Compensation reverses only THIS invocation's delta.** Pre-existing state
   → `already_satisfied` no-op, no compensation.
10. **Public content writes require frozen, token-bound intent.** Normalized
    text + SHA-256 bound to confirmation token. No mutation after confirmation.
11. **Composer DOM read-back before submit.** The last pre-submit assertion
    proves what the browser is about to submit.
12. **Final kill switch before irreversible submit.** "Hand on the button."
13. **One invalid manifest item rejects the entire invocation.** No partial
    uploads, no partial posts. Any failure before submit → abort-and-cleanup.
14. **Verification records the basis of proof.** When DOM evidence is
    unavailable (e.g., quote target), record `verified_by=execution_path`
    honestly. Don't claim DOM verification that isn't possible.

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| **0a** | session + whoami + health + envelope + journal + kill switch + read-only broker | **LIVE-VERIFIED** |
| **0b** | write-safety kernel (token-bound confirmation, 4-tier risk, global+per-action limits, dedupe) | **DONE** (203 tests) |
| **1** | golden read (`read <post_url>`) | **LIVE-VERIFIED** |
| **1b** | quote-tweet impl, display_name fix, unavailable-post handling | **DONE** |
| **2** | read_profile (fan-out) | **LIVE-VERIFIED** |
| **2b** | read_thread (conversation slice) | **LIVE-VERIFIED** |
| **2c** | read_search | **LIVE-VERIFIED** |
| **3** | bookmark_post (first write) | **LIVE-VERIFIED** |
| **3b** | like_post (public engagement, execution + compensation verified) | **LIVE-VERIFIED** |
| **4a** | compose_post (dry-run only) | **VERIFIED** |
| **4b** | post_text (first live public post) | **LIVE-VERIFIED** |
| **4c** | reply_post (target-bound, thread-aware verification) | **LIVE-VERIFIED** |
| **4c-v** | reply verification patch (thread-aware, post-id-aware) | **LIVE-VERIFIED** |
| **4d** | quote_post (execution-path verified, DOM limitation documented) | **LIVE-VERIFIED** |
| **4d-v** | quote attachment verification patch | **LIVE-VERIFIED** |
| **5** | analytics (separate adapter family) | not started |
| **v0.2 M1** | post_photo (media upload with 8 safety concerns) | **LIVE-VERIFIED** |
| **v0.2 M2** | download_image (separate DownloadBroker, HTTP fetch) | **LIVE-VERIFIED** |
| **v0.2 M3a** | reply_photo (target-scoped + media, composition atomicity) | **DONE** (verification gap flagged) |
| **v0.2 M3b** | quote_photo (dual attachment, identity-aware capture) | **LIVE-VERIFIED** |
| **v0.2 M4a** | post_multi_image (ordered media-manifest transaction) | **LIVE-VERIFIED** + runtime tests |
| **v0.2 M4b** | reply_multi_image | next |
| **v0.2 M4c** | quote_multi_image | after M4b |

## Capabilities (17)

| Capability | Tier | Status | Notes |
|-----------|------|--------|-------|
| whoami | read | LIVE-VERIFIED | @infaag resolved via Profile-link href |
| read | read | LIVE-VERIFIED | Single post: id/handle/created_at/text/metrics |
| read_profile | read | LIVE-VERIFIED | Fan-out: enumerate profile posts + retweet provenance |
| read_thread | read | LIVE-VERIFIED | Conversation slice: target/ancestors/replies, honest coverage |
| read_search | read | LIVE-VERIFIED | Search X: top/latest/people/media/lists tabs |
| download_image | read | LIVE-VERIFIED | Separate DownloadBroker (HTTP fetch, not ReadOnlyBroker) |
| health | read | LIVE-VERIFIED | Diagnostic: kill_switch/browser/broker/x_reachable/selector_readiness |
| bookmark_post | write | LIVE-VERIFIED | PRIVATE_REVERSIBLE tier |
| like_post | write | LIVE-VERIFIED | PUBLIC_REVERSIBLE_ENGAGEMENT, directional compensation verified |
| compose_post | write | VERIFIED | Dry-run only, no submit path |
| post_text | write | LIVE-VERIFIED | posted_and_verified |
| reply_post | write | LIVE-VERIFIED | reply_posted_and_target_verified (thread-aware) |
| quote_post | write | LIVE-VERIFIED | quote_posted_and_target_verified (execution-path) |
| post_photo | write | LIVE-VERIFIED | 8 media safety concerns, posted_and_verified + media_attachment_verified |
| reply_photo | write | DONE | Reply posted, composition atomicity verified, URL capture gap |
| quote_photo | write | LIVE-VERIFIED | Dual attachment (quote+media) verified separately, identity-aware capture |
| post_multi_image | write | LIVE-VERIFIED | Ordered manifest, exact-count, abort-cleanup, media_batch_verified |

## Safety kernel

- **Token-bound confirmation:** intent_hash binds capability + text + media hashes + target_post_id
- **4-tier risk classification:** PRIVATE_REVERSIBLE / PUBLIC_REVERSIBLE_ENGAGEMENT / PUBLIC_AMPLIFYING_REVERSIBLE / PUBLIC_CONTENT_IRREVERSIBLE
- **Global + per-action token bucket:** prevents runaway loops
- **Journal-hydrated dedupe:** semantic key (actor+action+target+variant), survives restart
- **Directional compensation:** semantic methods, not toggles; only reverses this invocation's delta
- **Text normalization:** deterministic canonicalization, bound to token
- **Media manifest:** ordered, immutable, SHA-256 per item, preflight-all-before-any-upload
- **Composer read-back:** DOM text verified before submit (caught 2 real bugs)
- **Final kill switch:** checked immediately before irreversible submit
- **Identity-aware post-submit verifier:** records pre-submit IDs, polls for new ID
- **Abort-and-cleanup:** any failure before submit → close composer, navigate away

## Broker surfaces

| Broker | Purpose | Methods |
|--------|---------|---------|
| ReadOnlyBroker | Read-only (no side effects) | navigate, observe, extract, query_attr, query_text, enumerate_posts, scroll, list_tabs, switch_tab |
| WriteBroker | Write mutations (only via WriteKernel) | click_bookmark, click_like, click_unlike, fill_composer, read_composer_text, click_submit, capture_posted_url, open_reply_on_target, fill_reply_composer, open_quote_on_target, fill_quote_composer, attach_media, verify_attachment_ready, count_attachments, close_composer |
| DownloadBroker | Local filesystem output (separate boundary) | resolve_post_image_url, download_image |

## Module map (src/webwire/)

| File | Role |
|------|------|
| `config.py` | WebWireConfig (immutable) |
| `session.py` | SessionManager — owns SuperBrowser lifecycle + cookie persistence |
| `envelope.py` | ActionResult adapter + failure constructors |
| `broker.py` | ReadOnlyBroker — read-only DOM surface |
| `write_broker.py` | WriteBroker — narrow write surface (one method per action) |
| `download_broker.py` | DownloadBroker — local-output boundary for media |
| `dispatcher.py` | Single entry point: invoke(name, input) |
| `registry.py` | Tier-gated capability registry |
| `journal.py` | Append-only NDJSON journal |
| `ports.py` | Narrow write-port protocols (BookmarkWritePort, LikeWritePort, PostWritePort) |
| `safety/kill_switch.py` | Flag + hot file + boot env |
| `safety/models.py` | WriteIntent, RiskMeta, ConfirmationToken, PolicyDecision |
| `safety/risk_registry.py` | DEFAULT_REGISTRY with all action types |
| `safety/token_bucket.py` | Per-action + global circuit breaker |
| `safety/dedupe.py` | Journal-hydrated semantic dedupe |
| `safety/write_kernel.py` | WriteKernel pipeline + WriteCapability protocol |
| `safety/text_normalize.py` | Deterministic text normalization + hash |
| `safety/attachment.py` | Attachment model + media validation (SHA-256, MIME, EXIF) |
| `safety/media_manifest.py` | Ordered media manifest + preflight |
| `safety/post_submit.py` | Identity-aware post-submit verifier |
| `capabilities/*.py` | 17 capability implementations |

## Recent commits

- `e44e55c` — M4a tests: 16 media manifest tests
- `0a10465` — M4a post_multi_image: ordered media-manifest transaction
- `de6c7a3` — M3b quote_photo + identity-aware post-submit verifier
- `b7e1569` — M3a reply_photo: reply with text + image
- `a5561de` — M2 download_image: separate local-output boundary
- `74bd088` — M1 post_photo: photo upload with media safety pipeline
- `f1921b2` — Phase 4d-v: quote attachment verification
- `b419f91` — Phase 4c-v: thread-aware reply verification
- `50096dc` — Phase 4d quote_post
- `b91a5c6` — Phase 4c reply_post

## Known gaps / open items

- [ ] M4b reply_multi_image (ChatGPT decision: B-with-gate — shared harness, M4a runtime tests must be green first; ✅ done)
- [ ] M4c quote_multi_image
- [x] ~~M4a runtime test gap (ChatGPT blocker)~~ — closed: 7 runtime tests added (attach-fail, count-mismatch, preview-not-ready, composer-mutation, kill-before-submit, transcoding-honest, rendered-order)
- [ ] reply_photo URL capture gap (submit_clicked_verification_pending — identity-aware verifier should be retrofitted)
- [ ] Phase 1b edge cases (quote-tweet, media-only, reply) need real fixtures
- [ ] Screenshot capture plumbed but unimplemented
- [ ] No CLI yet
- [ ] Cookie-only persistence (fragile; re-login on token expiry)
- [ ] Profile display_name extraction unreliable (testid may differ)
- [ ] Super-Browser download() Patchright bug (bypassed in DownloadBroker)
- [ ] Future: persistent-context feature in Super-Browser
- [ ] Future: follow/unfollow capability
- [ ] Future: delete/unpost capability
- [ ] Future: Phase 5 analytics

## Test fixtures

- **Test images:** `.webwire/test-media/test_red.png` (200x200 red), `.webwire/test-media/test_blue.png` (150x150 blue)
- **Test target post:** `https://x.com/infaag/status/2075426870883954690` (M1 photo post — Phase 4b post was DELETED from X)
- **Session:** `.webwire/session.json` (22 cookies incl. auth_token, for @infaag)

## History

- 2026-07-12: M4a runtime blocker closed. 7 failure-path tests added (test_post_multi_image_runtime.py) — ChatGPT's 7 enumerated cases now covered. 203 tests. ChatGPT decision: B-with-gate (shared harness, M4a green before M4b live).
- 2026-07-12: v0.2 M1-M4a complete. 17 capabilities, 196 tests, 27 commits. Media pipeline (post_photo, download_image, reply_photo, quote_photo, post_multi_image) with full safety (manifest, SHA-256 binding, EXIF, abort-cleanup, identity-aware verifier). ChatGPT bridge diagnosis delivered to PM.
- 2026-07-09: Phase 4 complete (post_text, reply_post, quote_post — all live-verified). v0.1 tagged.
- 2026-07-08: Phase 0a-LIVE PASSED through Phase 3b. Session persistence, write-safety kernel, bookmark + like live-verified.
