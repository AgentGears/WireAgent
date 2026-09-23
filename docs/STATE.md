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

**v0.3 (stabilized; M5 design FROZEN)** — 20 capabilities, 279 tests, CI-enforced.
Next: M5 per docs/M5_DESIGN.md (normative; baseline 7d081b9).
Type- and lint-clean; all media writes share ONE harness; offline stub SDK powers CI.
README rewritten to v0.2 truth; pyproject 0.0.1 → 0.2.0.
Health capability-selector probes landed (history i).
All P0/P1 review items + M4b landed and live-validated 2026-09-22. M4c
(quote_multi_image) is next. See History.

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
15. **The journal is the single source of truth for write safety; both memory
    stores rebuild from it on start.** Dedupe keys AND token-bucket budgets
    hydrate from journaled write facts (`capability_tier`, `action_type`,
    `risk_tier`, `dedupe_key`), so a restart during a loop resets neither
    guard. A dedupe key is journaled only when the kernel recorded the write:
    success, or an uncertain submit (`public_side_effect=True`) — for
    irreversible writes, "we don't know" is treated as "it happened." The
    memory layers fail open (missing/corrupt journal → empty stores, full
    budgets); the confirmation gate never depends on the journal. The journal
    is never rewritten — rotated whole at 10 MB / 31 days, 6 files retained.

## M5 — Effect Transaction Boundary (design FROZEN)

The full contract lives in **docs/M5_DESIGN.md** — normative and
self-contained; build from it, not from this section.

Provenance, recorded once: M5 arose from a seven-document adversarial
review loop (external deep dive → verification against the tree →
convergence → refinement → empirical correction → freeze request →
final precision). Every code claim in the chain was checked against the
tree; two live probes established the bookmark facts (both testid
buttons coexist on a bookmarked post; the bookmark click is a no-op when
already bookmarked — the destructive fallback in `click_bookmark` is
code-real, live-dormant, and fixed in commit 1).

**Architectural decision (retires an old recommendation):** approval
grants are intentionally ephemeral. Durable safety state belongs to the
EffectLedger, not to persisted confirmation tokens. The earlier P2 item
"persist and prune confirmation tokens" is obsolete — it solved the
wrong problem.

The change rule is part of the freeze: revise the design only when
implementation, fault injection, or live evidence falsifies an
invariant or assumption.

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
| **v0.2 M4b** | reply_multi_image (shared media-compose harness) | **LIVE-VERIFIED** + runtime tests |
| **v0.2 M4c** | quote_multi_image (shared harness, quote hook) | **LIVE-VERIFIED** (quote 2102520857155522777) |

## Capabilities (19)

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
| reply_multi_image | write | LIVE-VERIFIED | Shared harness, reply hook (target-first), thread-target verified, honest codes |
| quote_multi_image | write | LIVE-VERIFIED | Shared harness, quote hook; dual attachment reported separately (quote by execution path, media 2/2 DOM-verified) |
| delete_post | write | LIVE-VERIFIED | Compensation made real; id-scoped, kill-before-confirm, tombstone verify (deleted throwaway 2102523479664804123) |

## Safety kernel

- **Token-bound confirmation:** intent_hash binds capability + text + media hashes + target_post_id
- **4-tier risk classification:** PRIVATE_REVERSIBLE / PUBLIC_REVERSIBLE_ENGAGEMENT / PUBLIC_AMPLIFYING_REVERSIBLE / PUBLIC_CONTENT_IRREVERSIBLE
- **Registry gate (2026-09-22):** kernel policy runs on REGISTRY truth — unknown
  action_types denied; declared meta must match the registry entry exactly
- **Verify honesty (2026-09-22):** verify only after successful execute;
  "unknown" state reads are failures, not passes — ok=True means verified
- **Dry-run exception (2026-09-22):** side-effect-free executes (dry_run=True)
  record no dedupe key — preview flows never block the real post
- **Actor binding (2026-09-22):** whoami-resolved handle is the dedupe actor;
  persistence never sets identity
- **Global + per-action token bucket:** prevents runaway loops
- **Journal-hydrated dedupe AND budgets (2026-09-22 fix):** journal write-facts
  rebuild both stores on start; uncertain public submits record their key;
  rotation-aware tail-scan; torn tail warns and the rest hydrates
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
| `safety/media_compose.py` | Shared media-compose harness (M4a/M4b transaction, target-context hook) |
| `safety/media_verify.py` | Shared post-submit verifiers (id-scoped article selection, render polling) |
| `safety/post_submit.py` | Identity-aware post-submit verifier |
| `capabilities/*.py` | 18 capability implementations |

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

- [x] ~~P1: health selector probes stale (3/5 failed on a healthy logged-in
  session; `ready` stayed true)~~ — fixed 2026-09-22: probes are DOM-direct
  (new broker.probe_selectors, live-verified selector alternatives) and POLL
  until hydration — the root cause was probing BEFORE X's client-side render,
  not merely stale selectors; `ready` now gates on the 3 core probes;
  supplementary failures surface without gating; the not-ready path now
  attaches the diagnostic blob (was dropped). Live: 5/5, ready=true, 3.8s.
- [x] ~~P0: dedupe hydration broken (hydrated 0 entries — reader/journal field
  mismatch, untested)~~ — fixed 2026-09-22: write facts journaled, both stores
  hydrate, budgets survive restart, 9 tests in tests/test_hydration.py
- [x] ~~P0: media capabilities bypass per-action rate limits (`post_photo`,
  `post_multi_image`, `reply_photo`, `quote_photo` absent from TokenBucket
  defaults AND risk registry — no_limit path; only global 20/5min applies)~~ —
  fixed 2026-09-22 by NORMALIZATION, not registration: media capabilities use
  the BASE action_type (post/reply/quote), so a photo post draws from the same
  3/hour post budget as a text post (separate buckets would fragment budgets —
  the mixed-action-loop weakness the bucket design itself warns about). Media
  identity stays in semantic_variant (digests, already present). Guarded by
  tests/test_write_config_consistency.py: every WRITE capability's action_type
  must have bucket + registry entries; media caps must normalize; dedupe keys
  must stay media-distinct; 3 posts of ANY kind exhaust the post budget.
- [x] ~~P0: kernel never consults injected RiskRegistry~~ — fixed 2026-09-22:
  the kernel's policy stage now gates on the REGISTRY (step 2b): unknown
  action_type → denied (`unknown_action`); declared RiskMeta/CompensationMeta
  that differs from the registry entry → denied (`risk_meta_mismatch`).
  Fail-closed on drift or downgrade; no silent override. Guarded by
  tests/test_kernel_risk_gate.py + the meta-matches-registry consistency test.
- [x] ~~Actor identity `_resolved_handle` never set~~ — fixed 2026-09-22:
  whoami binds the handle via `SessionManager.set_resolved_handle` (dispatcher
  `_post_whoami_hook`); every write's dedupe key now carries the real actor.
  Persistence never sets identity — whoami remains the only authority.
- [x] ~~Verify-stage honesty~~ — fixed 2026-09-22: verify runs ONLY after a
  successful execute (`verify_skipped_execute_failed` stage otherwise); and
  bookmark/like verify treats `unknown`/`not_bookmarked`/`not_liked` states as
  verification FAILURES, not passes (ok=True now means verified).
- [x] ~~compose_post no-op recorded dedupe~~ — fixed 2026-09-22: executes that
  declare themselves side-effect-free (`dry_run=True`) record NO dedupe key;
  a confirmed dry-run no longer blocks the same-text post_text for the TTL.

- [x] ~~M4b reply_multi_image~~ — DONE 2026-09-22: shared harness extracted
  (safety/media_compose.py — M4a delegates, tests unchanged, 237 green at the
  refactor commit); reply_multi_image on it with the target-first hook; 10
  runtime tests; live-verified (reply 2102493233989529629, thread-target
  verified, media_count 2 after the verifier fix below).
- [x] ~~Bookmark mutation-as-probe (latent invariant-8 violation, found by
  the review chain 2026-09-23)~~ — FIXED in pre-M5 commit 1. The pre-fix
  click_bookmark used the removeBookmark click as a fallback probe: on an
  already-bookmarked post it would REMOVE the bookmark and report
  already_bookmarked. Live-probed status at discovery: code-real,
  live-DORMANT (both buttons coexist when bookmarked; primary click is a
  no-op) — retry-safe by DOM accident, never by code guarantee. Banked DOM
  facts (2026-09-23, fixture post): bookmark/removeBookmark COEXIST in the
  bookmarked state; the bookmark click is idempotent-when-bookmarked. Fix:
  state-read-first branching (not_bookmarked→click; bookmarked→
  already_satisfied zero-mutation; unknown→honest failure zero-mutation),
  mutual exclusion with the new click_remove_bookmark mirror, broker-level
  four-state tests over both topologies (tests/test_bookmark_semantics.py).
- [x] ~~M4c quote_multi_image~~ — LIVE-VERIFIED 2026-09-23 (quote 2102520857155522777:
  posted_and_target_verified, media_count 2/2, blob-count gates exact through
  the quote composer, dual attachment honest; ran after the budget window
  freed — journal-verified 0 quote events in the hour before the run)
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
- [ ] Future: Phase 5 analytics

## Test fixtures

- **Test target post (CURRENT):** `https://x.com/infaag/status/2102451358305771541`
  (2026-09-22 fixture post, posted via post_text as the P0 live validation —
  posted_and_verified, read-back confirmed. The prior M1 fixture post
  `2075426870883954690` was DELETED from X; @infaag profile had 0 posts.)
- **Test images:** `.webwire/test-media/test_red.png` (200x200 red), `.webwire/test-media/test_blue.png` (150x150 blue)
- **Session:** `.webwire/session.json` (22 cookies incl. auth_token, for @infaag; verified working 2026-09-22, 2+ months old)

## History

- 2026-09-23 (n): Pre-M5 commit 1 — bookmark made semantic and
  state-preserving. click_bookmark reads state first and never touches
  removeBookmark (the mutation-as-probe defect is gone); click_remove_bookmark
  added as its mirror (compensation metadata now names a real method). 7
  broker-level tests: the four observable rows across both DOM topologies,
  the swap-topology regression, the selector-churn-proof replay property
  (pre=bookmarked → post=bookmarked, zero mutations), and mutual exclusion.
  Suite 279.
- 2026-09-23 (m): M5 design FROZEN and recorded (docs/M5_DESIGN.md; baseline
  7d081b9). Twelve invariants, unified approval-spend rule (fenced: durable
  EFFECT_RESERVED is the authoritative spend event — the ledger fact dominates
  the transient object; non-fenced: the gateway's process-local CAS), four-axis
  EffectPolicy (risk / authority / ReplaySemantics / durability; UNKNOWN =
  conservative), ApprovalGrant/EffectAttempt as separate state machines with an
  orthogonal claim lock, authorization-epoch kill revocation, effect-knowledge
  states (COMMIT_ATTEMPTED demoted to diagnostic event), enforcement-grade
  RecoveryGuard, journal/ledger split, T1-T14 acceptance tests, seven-step
  build order. Negative guarantees recorded to prevent claim inflation.
  Sequence from here: bookmark semantic fix → metadata chore → M5 layers.
- 2026-09-23 (l): STABILIZATION BATCH complete. New home pushed (46-commit history,
  verified secret-free: sweep + full-blob scan of tracked tool-state; remote's
  wizard stub replaced). C1 CI foundation: offline stub of the Super-Browser SDK
  (tests/stubs — every SuperBrowser method fails LOUDLY), conftest activates it
  only when the real SDK is absent, test_stub_parity enforces shape parity on dev
  machines; pyproject deps emptied (SDK → [browser] extra); scripts/check.sh gate;
  GitHub Actions (3.11/3.12). C2 ruff: 264 findings → 0 (real finds: duplicate
  started property, blocking HTTP in download_broker, zip strictness, blind
  raises narrowed). C3 mypy: 117 → 0 (real finds: RiskRegistry.require() —
  compose fails loud on unknown actions; the registry/protocol union lie fixed;
  16 str=None annotation bugs; 50 stale ignores stripped). CI caught a real
  environment split on run 1 (PIL optional) — fixed with override + getattr-probe.
  C4 single-photo migration: post/reply/quote_photo delegate to the shared
  harness (zero harness changes; M4a/M4b/M4c suites untouched), single-item paths
  GAIN the exact-count gate (accepted tightening, test-locked); local helper
  duplication deleted; capture adapters preserve historical capture semantics.
  Suite 272; gate (pytest+ruff+mypy) fully green; parked list RETIRED.
  Process lesson twice-learned: never pipe the gate through tail.
- 2026-09-23 (k): delete_post LIVE-VERIFIED (capability a2c9218 + fixes). The
  compensation metadata is now an executable: registry 'delete_post' entry
  (irreversible, de-amplifying → conservative tier), 10/hour bucket, WriteBroker
  delete port (id-scoped article → caret → text-matched 'Delete' menu item →
  confirmationSheetConfirm, kill re-checked before the confirm, dialog
  dismissal on every failure), honest read_post_state (present|deleted|
  unknown). Live: throwaway post created via post_text, then deleted through
  the full kernel — post_deleted, verify_ok=true (tombstone), zero residue.
  THREE live-caught bugs during landing: (1) _delete_poll double-wrapped the
  poll coroutine (never awaited — every stage failed); (2) preview's
  single-shot existence probe fired pre-hydration (a just-created post read
  'absent') — now polled; (3) missing asyncio import in the new methods
  (the file imports per-method; NameError surfaced only at the first sleep).
  Diagnostic discipline paid: caret/menu stages were proven read-only BEFORE
  any delete click (menu dump: 13 items, 'Delete' first).
- 2026-09-23 (j): M4c LIVE-VERIFIED — quote_multi_image posted and fully
  verified against the fixture post (status/2102520857155522777): text
  verified, media_count 2/2 (the blob-first count gates exact inside the
  quote composer), quote attachment by execution path, actor 'infaag|'
  in the dedupe key, write facts journaled. The run executed only after a
  journal-timestamp check confirmed 0 quote events in the preceding hour.
  THE v0.2 MEDIA TRANCHE (M1-M4c) IS COMPLETE: 19 capabilities.
- 2026-09-23 (i): Health capability-selector probes (the re-evaluation's top
  residual risk, closed). New supplementary check `capability_selectors` —
  tweetText, bookmark/removeBookmark, inline composer — rides the same
  probe_selectors round-trip, strict-aggregated, NEVER gating ready. Live
  battery first (json.dumps embedding): tweetButton excluded (modal-only,
  statically absent), tweetPhoto excluded (content-dependent — presence
  depends on feed contents, not app structure). The first live run caught a
  NEW race: shell landmarks render BEFORE feed articles, so shell-exit
  polling reported capability selectors false on a healthy feed — the poll
  now waits for BOTH sets (same deadline); shell still solely gates ready.
  Live: shell 5/5, capability 3/3, ready=true. Suite 258 (+2 tests).
- 2026-09-22 (h): M4c CODE COMPLETE (commit 4036cb3; harness quote-hook extension
  additive, 247-green neutrality; 9 runtime tests; suite 256). LIVE attempt:
  the exact-count gate ABORTED CLEANLY (expected 1 after upload 1, got 2;
  public_side_effect=false, nothing posted) — M4c was the first multi-image
  flow inside a QUOTE composer, and count_attachments over-counted there:
  the quote composer renders the QUOTED AUTHOR'S AVATAR (pbs.twimg.com img)
  inside the attachments container alongside the blob: upload (diagnosed
  live via structure+src dumps, quote vs reply). FIX: count_attachments now
  counts blob: uploads first (legacy img-counts as fallback). Live-proven
  against the exact failing gate: quote composer 1→2 per upload, reply 1.
  FULL two-phase public quote DEFERRED: the failed attempt consumed 2 of
  the 3-per-hour quote budget; a full rerun needs 2 — run it when the
  window frees (~1h). Diagnostics: scripts/diag_m4c_*.py.
- 2026-09-22 (g): M4b COMPLETE. Shared media-compose harness extracted
  behavior-preservingly (neutrality gate: all 237 tests green with ZERO
  test changes; M4a demonstrably delegates); reply_multi_image built on it
  (action_type 'reply', target-first hook, thread-target verification,
  honest result codes); 10 runtime tests (suite 247). LIVE: two-phase
  reply with 2 images on the fixture post — posted, thread-target verified,
  actor 'infaag|' in key, write facts journaled. Live run caught TWO shared-
  verifier bugs: (1) first-article scoping — on a reply permalink the first
  article is the PARENT (verifier read the wrong post; images were posted
  fine — 2 tweetPhotos on the reply's article); (2) fixed-sleep hydration
  race. Fixed: verifiers now scope by the posted status id and poll until
  render; re-verified live read-only (text_verified=True, media_count=2).
  The original run's honest mismatch result stands in the journal.
- 2026-09-22 (f): P1 health probes fixed. Root cause doubled: probes ran
  BEFORE X's client-side render (everything false on a healthy session) AND
  inferred from the observe() snapshot. Now: broker.probe_selectors (new
  bounded read primitive, one round-trip, selector groups with alternatives
  live-verified via diag_selector_battery_live.py), hydration polling with
  deadline (early-exit on full pass), core-probe gate on ready, diagnostic
  attached on failure. Live: 5/5, ready=true, 3.8s. 237 tests. First test
  file for health (test_health.py — 7 tests).
- 2026-09-22 (e): P1 fixture post landed via post_text — posted_and_verified
  (status/2102451358305771541), read-back confirmed; doubles as live
  validation of all four P0 batches (registry gate passed, write facts
  journaled, posted key hydrated). LIVE RUN CAUGHT A BUG the offline suite
  missed: the post-whoami hook was called without await, so actor binding
  never ran (first post journaled a '?' actor). Fixed; regression test drives
  the real invoke() path; re-verified live (phase-1 only) — dedupe key now
  'infaag|post|...'. 230 tests. Old M1 fixture post confirmed deleted from X.
- 2026-09-22 (d): Final kernel-hygiene batch — actor identity bound from
  whoami (dedupe keys carry the real handle); verify gated on execute success
  with honest state semantics; dry-run no-ops exempt from dedupe recording.
  One existing test (compose dedupe-blocks-replay) rewritten: it encoded the
  preview→post-blocking quirk this batch removes. 229 tests. All P0 done.
- 2026-09-22 (c): P0 gap 3 closed. Kernel policy stage now verifies writes
  against the risk registry (unknown_action / risk_meta_mismatch denials);
  self-declared risk metadata is no longer trusted. Sample-input table moved
  to tests/conftest.py. 221 tests.
- 2026-09-22 (b): P0 media rate-limit bypass closed. Media action_types
  normalized to base actions (post/reply/quote) — media posts now count
  against the same per-action budgets as text posts; config-consistency tests
  added (would have caught the original bug). 216 tests.
- 2026-09-22: Architecture review + live E2E. Review found 3 verified gaps
  (dead dedupe hydration, media rate-limit bypass, unused risk registry); E2E
  proved substrate alive (session survived 2+ months; control reads pass) but
  fixture post deleted from X. P0 hydration fix implemented same day: journal
  write facts (capability_tier/action_type/risk_tier/dedupe_key), both stores
  hydrate from one shared tail-scan read, uncertain-submit recording rule,
  rotation (10MB/31d, retain 6), fail-open documented as invariant 15. 212 tests.
- 2026-07-12: M4a runtime blocker closed. 7 failure-path tests added (test_post_multi_image_runtime.py) — ChatGPT's 7 enumerated cases now covered. 203 tests. ChatGPT decision: B-with-gate (shared harness, M4a green before M4b live).
- 2026-07-12: v0.2 M1-M4a complete. 17 capabilities, 196 tests, 27 commits. Media pipeline (post_photo, download_image, reply_photo, quote_photo, post_multi_image) with full safety (manifest, SHA-256 binding, EXIF, abort-cleanup, identity-aware verifier). ChatGPT bridge diagnosis delivered to PM.
- 2026-07-09: Phase 4 complete (post_text, reply_post, quote_post — all live-verified). v0.1 tagged.
- 2026-07-08: Phase 0a-LIVE PASSED through Phase 3b. Session persistence, write-safety kernel, bookmark + like live-verified.
