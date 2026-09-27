# M7 — Cross-Process Authority & Ownership Boundary

```text
Status:   DESIGN CANDIDATE — MAINTAINER-FIRST REVIEW; NO IMPLEMENTATION YET
Base:     main f3268407167ad39f6be1aa01b4b8dc34cef59da7
Runtime:  M5 transaction boundary complete; M6 reconciliation/qualification Layers 1–7 complete
Scope:    same-machine, same-user, multiple cooperating WireAgent processes sharing one canonical state directory;
          exactly one production authority owner, local IPC clients, crash-safe takeover after owner death
Change rule: preserve M5/M6 semantics; revise this design only when source review, implementation evidence,
             fault injection, platform qualification, or an independently verified review finding falsifies an assumption
```

This document is normative and self-contained for M7. Build M7 from this file,
not from review-conversation memory.

`docs/M5_DESIGN.md` remains authoritative for effect transaction semantics.
`docs/M6_DESIGN.md` remains authoritative for reconciliation, confirmation-epoch,
composite recovery, and replay-qualification semantics. M7 does not reopen those
contracts merely because it adds a process boundary around them.

The M7 forcing function is narrow:

> M5/M6 are deliberately safe inside one Python process, but the current process-
> local locks, confirmation state, browser lease, and publication fences do not
> coordinate two independent WireAgent runtime/recovery processes that share one
> state directory.

M7 resolves that forcing function by **centralizing authority**, not by turning
every M5/M6 primitive into a distributed object.

---

## 1. Maintainer-first source findings

The first pass reviewed the exact M6-complete baseline before choosing a mechanism.
The following are architectural facts at `f3268407`, not defects in M5/M6.

### M7-F01 — EffectLedger writer coordination is process-local

`EffectLedger` shares a normalized-path `threading.RLock` between same-process
instances and explicitly states that independent external writers are outside its
single-process contract. Its fsync/history semantics are safety-critical, but the
file format itself is not a supported concurrent multi-process writer protocol.

**Consequence:** M7 must not assume that opening the same `effects.ndjson` from two
processes is safe merely because both processes use `EffectLedger`.

### M7-F02 — ConfirmationState authority is process-local

`ConfirmationState` owns one in-memory epoch, pending-token store, monotonic clock
sampling, and consume fence. M6 intentionally does not persist or reconstruct
human confirmation authority.

**Consequence:** replicating `ConfirmationState` into multiple processes would
create multiple independent human-authority roots. M7 should keep one canonical
instance in the authority owner process.

### M7-F03 — M6 reconciliation ordering is process-local

`ReconciliationPublicationFence`, `ReconciliationCoordinator` protocol state,
CommitGateway lifecycle ownership, and the committed-fact continuation map are
shared by normalized paths only inside one process. M6 explicitly excludes
independent processes.

**Consequence:** a separate recovery process must not append reconciliation while
a production owner is active. Recovery must either route through that owner or
become the exclusive owner after it exits.

### M7-F04 — browser write/read coordination is process-local

`M5LeasedWriteBroker` attaches an async lease state to the owned SuperBrowser
facade. `Dispatcher` additionally serializes supported invocations because reads
and writes share one browser. A second process has neither of those locks.

**Consequence:** M7 cannot solve only file writes. Production browser reads and
writes must have the same single process owner.

### M7-F05 — Dispatcher is already the natural authority root

`Dispatcher` owns the session, kill switch, WriteKernel, EffectLedger,
RecoveryGuard, CommitGateway, live M5 stack, and M6 ReconciliationCoordinator.
It hydrates recovery before launching/restoring the browser and serializes
supported invocations.

**Consequence:** the least invasive M7 design is an outer process-ownership
boundary around the existing Dispatcher/recovery authority root, not a rewrite of
M5/M6 internals.

### M7-F06 — owned browser remains the production path

`SessionManager` defaults to launch/owned mode. Attach mode is explicitly a
non-production diagnostic path and requires opt-in.

**Consequence:** takeover must launch a fresh owned browser authority. A successor
must not silently attach to an orphaned browser from a dead owner and call that
production authority.

### M7-F07 — M6 itself names cross-process coordination as the next forcing function

The M6 completion criteria explicitly state that multiple independent runtime or
recovery processes sharing one state directory are the forcing function for a
separate M7 authority/locking design.

### M7-F08 — time-based takeover would create the wrong safety model

A browser click or X service mutation does not carry a WireAgent fencing token
that the remote system can reject. Therefore a heartbeat/TTL lease that allows a
second process to take over while the first process is merely paused or hung can
produce two live mutation authorities.

**Design conclusion:** M7 must not use heartbeat expiry, PID age, lock-file mtime,
or wall-clock staleness to steal production authority. Availability yields to
single-owner safety.

---

## 2. Architecture decision — one authority process, many local clients

The supported M7 topology is:

```text
             local client A
                  |
             local client B
                  |
             CLI / agent / UI
                  |
                  v
        +-----------------------+
        |   AuthorityService    |
        |   one owner process   |
        |                       |
        | AuthorityOwnerLock    |
        | AuthoritySession      |
        | Dispatcher            |
        | M5 + M6 authority     |
        +-----------+-----------+
                    |
          +---------+----------+
          |                    |
          v                    v
    owned browser       safety ledgers
                       effects.ndjson
                       reconciliations.ndjson
```

Only the owner process may perform supported production browser operations or
safety-ledger mutations. Other M7-aware processes are clients. If no local IPC
service is enabled, a second process fails `authority_busy`; it does not create a
second authority root.

This is a **single-writer / single-browser-owner design**, not distributed
consensus.

### 2.1 Why centralize instead of adding locks everywhere

M5/M6 already have a coherent authority root and carefully ordered internal
locks. Adding unrelated cross-process file locks to EffectLedger,
ReconciliationLedger, ConfirmationState, RecoveryGuard, CommitGateway, browser
leases, and the operator workflow would create many new lock orders and still
would not fence the remote browser/X mutation seam.

M7 therefore adds one outer ownership law:

```text
AuthorityOwnerLock — held for the entire production authority lifetime
    -> existing Dispatcher / M5 / M6 lock orders remain unchanged
```

Within the supported topology, one owner process means the existing process-local
M5/M6 synchronization continues to be the canonical inner synchronization.

---

## 3. Scope and non-goals

### 3.1 In scope

M7 defines and qualifies:

1. one cross-process authority domain per canonical WireAgent state directory;
2. a non-expiring OS-held `AuthorityOwnerLock` for that domain;
3. a process-local `AuthoritySession` representing successful ownership;
4. one fresh random `authority_instance_id` per successful owner acquisition;
5. owner acquisition before any authoritative recovery/browser startup;
6. owner release only after supported mutation/browser authority is quiesced;
7. local IPC client access to the owner without exposing raw browser commands;
8. stale-client rejection across owner restart;
9. owner-side transport request dedupe that remains explicitly non-durable and
   non-authoritative;
10. shared owner-side human confirmation authority across all local clients;
11. owner-side M6 reconciliation across all clients;
12. crash/restart/takeover semantics that preserve M5/M6 durable uncertainty;
13. actual multi-process qualification, especially on Windows Server 2025;
14. bounded claim ceilings for local OS lock and IPC behavior.

### 3.2 Out of scope

M7 does **not** provide:

- multiple simultaneous production owners;
- heartbeat/TTL-based automatic failover while an owner process is alive;
- multi-machine coordination, leader election, quorum, or consensus;
- distributed exactly-once execution;
- a multi-writer EffectLedger or ReconciliationLedger file format;
- remote/network IPC service exposure;
- multi-user RBAC or hostile same-user process isolation;
- protection from Administrator/root modifying files or driving the browser;
- network-filesystem lock/durability semantics;
- durable human confirmation, ApprovalGrant, EffectPermit, or browser leases;
- replay-policy changes from M6;
- terminal reconciliation correction/supersession;
- durable cross-restart token-bucket or semantic-dedupe state;
- global uniqueness across two intentionally different WireAgent state roots that
  target the same remote X account;
- compatibility safety against an older pre-M7 binary running concurrently and
  ignoring the M7 ownership protocol.

---

## 4. Constitutional distinctions

M7 preserves these distinctions:

```text
lock-file existence       != authority ownership
PID                        != authority ownership
heartbeat freshness        != authority ownership
client connection          != execution authority
authority instance id      != durable effect identity
request id                 != effect id
transport dedupe           != semantic/effect replay safety
IPC response success       != external effect confirmation
process death              != proof of NO_EFFECT
owner takeover             != old-attempt continuation
browser process existence  != WireAgent browser authority
attach mode                != production ownership
recovery clear             != permission to execute
M7 owner lock              != M5/M6 policy approval
same state directory       != same remote actor across arbitrary configurations
```

Project-wide laws remain unchanged:

- **Cognition != authority.**
- **Execution != external effect.**
- **Evidence != claim.**
- **UNKNOWN != NO_EFFECT.**
- **Recovery clear != execution permission.**

---

## 5. Authority domain

One M7 authority domain is identified by the canonical configured `state_dir`.
The production implementation normalizes the path before registry lookup and lock
acquisition using the same case-normalization posture already used by the Windows
safety-ledger work.

Conceptually:

```text
authority_domain = canonical_local_path(WebWireConfig.state_dir)
authority_lock   = authority_domain / "authority.lock"
```

The lock file is a stable rendezvous object only. Its existence, contents, PID,
mtime, or age are never authority.

### 5.1 `AuthorityOwnerLock`

`AuthorityOwnerLock` has this contract:

- exclusive for one canonical authority domain;
- non-blocking acquisition is available so callers can return `authority_busy`;
- the underlying OS lock is held continuously for owner lifetime;
- the OS releases the lock on process death/handle closure;
- the lock is **not** time-expiring;
- the underlying handle is non-inheritable by child processes;
- a process-local normalized-domain registry also prevents accidental sibling
  authority roots inside the same Python process;
- acquisition/release errors fail closed;
- the lock file is not deleted as part of normal ownership transfer.

M7 requires a platform adapter with equivalent semantics on supported operating
systems. The exact primitive is implementation evidence, not an excuse to weaken
this contract.

### 5.2 No stale-file cleanup protocol

After a crash, `authority.lock` may still exist. The successor opens the same
rendezvous file and asks the OS lock primitive whether ownership is available.
It does not delete a supposedly stale lock file, inspect a PID, or compare an
mtime.

This removes stale-file deletion races and PID-reuse reasoning from safety.

### 5.3 `AuthoritySession`

Successful lock acquisition creates one process-local `AuthoritySession`:

```text
AuthoritySession
  authority_domain
  authority_instance_id
  acquired_at           # wall-clock provenance only
  active                # process-local terminal lifecycle flag
  lock_handle           # private; never serialized
```

`authority_instance_id` is a fresh cryptographically random 256-bit identifier.
It is runtime identity/provenance and an IPC stale-session fence. It is **not** an
EffectLedger lineage field and is not persisted as execution authority.

Releasing the session is terminal. A released session can never become active
again. Any owner-local operation that receives an inactive session fails closed.

### 5.4 Why M7 has no durable owner generation

A durable monotonically increasing owner generation is unnecessary for the M7
claim because M7 forbids live-owner stealing. The next owner can acquire only
after the old owner released the OS lock or the old process died. M5/M6 ephemeral
authority is never reconstructed after that boundary.

A fresh random `authority_instance_id` is sufficient to reject stale client
messages after restart without introducing a third durable safety history and its
own crash protocol.

If a future design wants takeover while an old process may still be alive, that
is a different problem. It would require a mutation seam that can actually reject
stale fencing generations; browser/X side effects currently provide no such
mechanism.

---

## 6. Owner startup and shutdown ordering

### 6.1 Startup

Supported owner startup is ordered:

```text
canonicalize/create local state directory
-> reserve process-local authority-domain slot
-> acquire OS AuthorityOwnerLock
-> mint AuthoritySession + authority_instance_id
-> construct canonical Dispatcher/M5/M6 authority root
-> hydrate/validate composite RecoveryGuard from both safety ledgers
-> if recovery unavailable/corrupt: fail closed before browser startup
-> start fresh owned browser/session
-> install coherent M5/M6 live stack
-> start local IPC listener last
-> accept client work
```

The owner lock is acquired **before** authoritative ledger hydration and before
browser startup. A losing process therefore cannot race far enough to construct a
second production mutation path.

If startup fails before a mutation-capable runtime is exposed, cleanup may release
the owner lock. A later owner must still perform normal M5/M6 hydration; startup
failure never implies clean external state.

### 6.2 Controlled shutdown

Controlled owner shutdown is ordered:

```text
stop accepting new IPC work
-> mark authority service draining
-> wait for / serialize behind the current Dispatcher invocation
-> advance/discard pending human confirmation authority for teardown
-> close owner-side operator sessions
-> stop/retire browser-capable live stack
-> close local IPC endpoint
-> mark AuthoritySession terminal
-> release AuthorityOwnerLock LAST
```

The process may continue running for diagnostics after release, but a terminal
`AuthoritySession` cannot authorize another production operation. Reacquisition
requires a new session and a complete new startup/hydration sequence.

A forced process death need not execute this sequence; M5/M6 durable recovery is
the crash path.

---

## 7. Takeover and hung-owner policy

M7 deliberately chooses **fail-stop takeover**, not time-based failover.

### 7.1 Clean takeover

A successor can become owner after the prior owner completes controlled shutdown
and releases the OS lock. It receives a new `authority_instance_id` and rebuilds
all ephemeral authority from scratch.

### 7.2 Crash takeover

If the owner process exits or is terminated, the OS must release the ownership
lock. The successor then performs the full startup ordering and rebuilds recovery
truth exclusively from durable M5/M6 histories.

Old confirmation tokens, ApprovalGrants, EffectPermits, claims, browser leases,
request-cache entries, and reconciliation authorities do not survive.

### 7.3 Hung owner

If the owner process is alive but hung, the lock remains owned. Another process
must receive `authority_busy` and **must not steal** ownership because a heartbeat
or deadline expired.

Operator recovery is explicit:

```text
observe owner unhealthy
-> trip kill hot file if useful
-> terminate the hung owner process if takeover is required
-> wait for OS ownership release
-> start successor normally
```

This is intentionally less available than a TTL lease. It prevents a paused old
owner from waking after a lease timeout and crossing an unfenceable browser/X
mutation seam.

### 7.4 Unexpected lock loss

If the platform adapter can detect that an active owner no longer holds its OS
lock, the owner must immediately make its AuthoritySession terminal, trip/fail
closed locally, stop accepting IPC work, and refuse new browser mutation
authority. It must not silently reacquire and continue the old authority session.

---

## 8. Local IPC boundary

M7 adds a local client/server boundary so additional processes do not need their
own Dispatcher or browser.

### 8.1 Transport constraints

The production transport is local-machine only:

- Windows: a qualified local IPC primitive such as a named pipe;
- POSIX: a qualified local IPC primitive such as a Unix-domain socket;
- no TCP listener in the M7 production path;
- endpoint permissions are restricted to the current user where the platform
  exposes that control;
- no Python pickle or other executable object deserialization is permitted;
- request/response frames use bounded, strict, non-executable serialization such
  as UTF-8 JSON with explicit schemas and size limits.

Transport choice may vary by platform, but the authority semantics above it do
not.

### 8.2 Handshake

A client first receives a bounded handshake:

```text
AuthorityHello
  protocol_version
  authority_instance_id
  runtime_version
  supported_capabilities
  state: ready | draining | killed | recovery_unavailable
```

The handshake is diagnostic/protocol state. It is not execution authority.

### 8.3 Request envelope

Every client request includes:

```text
AuthorityRequest
  protocol_version
  authority_instance_id
  request_id
  operation
  payload
```

Rules:

- `authority_instance_id` must exactly match the live owner;
- `request_id` is a fresh opaque client request identifier;
- unknown operation/schema/version fails before Dispatcher/recovery invocation;
- stale owner instance fails `stale_authority_instance`;
- clients never receive raw SuperBrowser, CommitGateway, EffectLedger, or
  ReconciliationLedger mutation surfaces.

### 8.4 Owner-side request table

The owner maintains an **ephemeral transport request table** keyed by
`request_id` for its current authority instance.

For one request id:

```text
new + canonical payload       -> execute once
same id + same in-flight fact -> join/await or return request_in_progress
same id + same completed fact -> return same cached response
same id + different fact      -> protocol violation; no execution
```

This table is process-local, bounded, and disposable. It is not a safety ledger,
not semantic dedupe, and not evidence that an external effect occurred.

After owner restart the table is empty. M5/M6, not the request cache, governs
uncertain external effects.

---

## 9. Browser ownership

M7 strengthens the existing owned-browser invariant:

1. only the active authority owner starts/owns the production browser session;
2. all production browser reads **and** writes route through that owner;
3. client processes do not directly navigate the production browser;
4. the current Dispatcher invocation lock and M5 browser lease remain the inner
   browser coordination mechanisms;
5. attach mode remains diagnostic and is not accepted as M7 production authority;
6. after owner crash, a successor does not silently attach to an orphaned browser
   process from the old owner;
7. the successor launches/restores a fresh owned session and re-establishes actor
   identity with the existing whoami authority rules.

An orphan browser process may still exist after abnormal termination, and a
remote request already emitted by it may complete. That is an external-effect
uncertainty problem already handled by M5/M6; browser-process survival is never
proof of either effect or no-effect.

---

## 10. M5/M6 integration

M7 adds one outer lifetime boundary. The current inner authority model remains:

```text
AuthorityOwnerLock                 # M7, entire owner lifetime
  -> Dispatcher invocation domain
     -> WriteKernel confirmation/policy shell
     -> RecoveryGuard
     -> M5 scoped authority
     -> CommitGateway
     -> EffectLedger
     -> browser effect/evidence

AuthorityOwnerLock                 # same owner
  -> M6 ReconciliationCoordinator
     -> ReconciliationPublicationFence
     -> CommitGateway lifecycle fence
     -> ConfirmationState epoch
     -> ReconciliationLedger
     -> RecoveryGuard publication
```

M7 does **not** add a second CommitGateway, ConfirmationState, browser lease, or
RecoveryGuard per client.

### 10.1 Safety ledgers remain single-writer in the supported topology

M7 does not redesign the NDJSON ledgers into arbitrary multi-process writer
formats. The supported rule is stronger and simpler:

> A process without the active AuthoritySession cannot use a supported production
> path that writes EffectLedger or ReconciliationLedger.

Raw imports of low-level safety classes by non-cooperating code remain outside
the M7 threat model, just as same-user hostile code is not a sandboxed tenant.

### 10.2 Existing durable semantics remain unchanged

Owner death during M5/M6 persistence uses the existing ledger laws:

- torn/corrupt history fails closed;
- raw `RESERVED` after restart projects to unresolved unknown;
- `EFFECT_UNKNOWN` remains immutable historical truth;
- ambiguous reconciliation remains non-authoritative until exact re-durability;
- old grant/permit/reconciliation authority is never reconstructed.

---

## 11. Human confirmation across processes

The owner process has exactly one canonical `ConfirmationState` for its authority
instance. All clients share that state indirectly through the owner.

```text
client A preview -> owner issues token T
client B preview -> owner issues token U
M6 reconciliation -> owner advances one confirmation epoch
T and U -> stale under the same owner-side state
```

Rules:

- clients hold only opaque token strings and preview data;
- token authority remains private owner state;
- a token is not bound to client process identity in M7's trusted same-user model;
- existing capability/intent/risk/single-use/monotonic-TTL bindings remain
  authoritative;
- terminal reconciliation advances the same owner-side epoch used by every
  client;
- owner restart destroys all pending token authority;
- a stale client also fails the new owner's `authority_instance_id` check before
  its old token could be treated as current protocol state.

M7 intentionally does not create a durable cross-restart confirmation epoch.
Restart has no pending confirmation authority to preserve.

---

## 12. Reconciliation across processes

M6 terminal reconciliation remains local human/operator authority. M7 changes
only where that workflow runs.

### 12.1 Owner active

When a production owner is active, any external client/operator workflow routes
through that owner. A second process may inspect/propose via local IPC but does
not construct an independent write-capable ReconciliationCoordinator for the same
authority domain.

Owner-side reconciliation uses the existing M6 publication, lifecycle,
confirmation-epoch, durability, and RecoveryGuard ordering unchanged.

### 12.2 Owner absent

A standalone local recovery tool may acquire `AuthorityOwnerLock` and become a
**temporary authority owner**. It must:

```text
acquire ownership
-> hydrate both safety ledgers
-> construct canonical M6 authority root
-> run the explicit operator workflow
-> start an owned browser only if bounded inspection actually requires it
-> release ownership through normal quiescent shutdown
```

If another owner is active, standalone recovery returns `authority_busy` and must
not write reconciliation directly.

### 12.3 Operator sessions over IPC

Any IPC operator session is owner-side, ephemeral, bound to the current
`authority_instance_id`, and closed on owner restart. The existing same-session
explicit human confirmation requirement for terminal reconciliation remains.

---

## 13. Kill semantics under M7

The current kill hot file remains the external same-state-directory emergency
mechanism. M7 does not turn kill into ownership transfer.

Rules:

- owner-side programmatic kill continues to use the existing KillSwitch;
- the shared hot file can be created by an external local process;
- the owner re-observes kill at the existing M5 authority boundaries;
- kill does not release `AuthorityOwnerLock`;
- kill does not authorize a successor while the owner remains alive;
- kill does not clear/rewrite M5/M6 history;
- owner restart while the hot file remains present starts in a mutation-blocked
  state under existing kill semantics.

A hot-file trip cannot forcibly interrupt an arbitrary OS/browser call already in
progress. M7 makes no impossible instantaneous cross-process cancellation claim.
If a hung owner must be replaced, the operator terminates the owner process; M5/M6
then govern any uncertain effect.

---

## 14. Client retry and lost-response semantics

Transport uncertainty must not become external-effect replay.

### 14.1 Owner remains alive

If a client loses a response and retries the **same** `request_id` against the
same `authority_instance_id`, the owner request table returns/joins the same local
request outcome rather than starting a second copy.

For confirmed writes, the existing single-use confirmation token and M5 attempt
boundary remain the deeper authority even if transport dedupe fails.

### 14.2 Owner dies before the client learns the result

A successor has a new `authority_instance_id` and an empty request table. The
client must not convert this into automatic fresh confirmation plus replay.

The correct flow is:

```text
old owner unavailable / response unknown
-> connect to successor
-> inspect current recovery/effect state
-> if durable uncertainty exists: remain blocked / reconcile
-> otherwise start a genuinely fresh human-approved invocation if desired
```

`request_id` is never persisted into M5/M6 as a substitute for `effect_id`.

---

## 15. Crash and restart semantics

### 15.1 Crash before M5 durable reservation

No old ephemeral execution authority survives. Successor starts fresh; any future
mutation requires normal fresh confirmation.

### 15.2 Crash after durable `RESERVED`, before known terminal outcome

Successor hydration projects the effect as unresolved unknown. Matching semantic
replay is blocked by RecoveryGuard. Owner transfer does not change that fact.

### 15.3 Crash after external mutation, before terminal evidence/outcome

Same result: durable uncertainty remains blocked/reconcilable under M5/M6.
Process death is not `NO_EFFECT` evidence.

### 15.4 Crash after durable terminal M5 outcome

Successor reconstructs the settled M5 effect history. No old grant/permit is
revived.

### 15.5 Crash during reconciliation

Existing M6 semantics apply exactly: confirmation authority dies with the process;
visible-but-ambiguous reconciliation cannot clear recovery; durable surviving
reconciliation is re-durability-established before use.

### 15.6 Crash with a surviving browser child process

The child browser is not authority. Successor does not attach to it as the M7
production session. Any already-emitted remote effect is represented only by M5/M6
evidence/recovery truth.

### 15.7 Machine reboot

All OS ownership locks and ephemeral authority vanish. A new owner acquires the
lock and rebuilds from durable M5/M6 state. M7 adds no reboot-persistent execution
authority.

---

## 16. Security and trust boundary

M7 remains a personal, local, single-user system.

Supported threat/failure model:

- multiple normal/cooperating WireAgent processes under the same local user;
- accidental concurrent startup;
- process crash/forced termination;
- stale client after owner restart;
- IPC disconnect/duplicate delivery/lost response;
- browser/runtime fault;
- existing ledger crash/corruption ambiguity handled by M5/M6.

Not protected against:

- malicious same-user code importing raw safety classes or driving its own
  browser automation;
- Administrator/root bypass of files, process handles, or IPC ACLs;
- malicious modification of safety ledgers;
- hostile kernel/filesystem behavior;
- remote multi-host races.

The IPC layer must not expand this boundary by opening a network listener or
using executable deserialization.

---

## 17. Path, platform, and storage claim ceilings

M7's ownership claim is for one canonical **local** state directory on a tested
platform/filesystem combination.

Initial claim ceiling excludes:

- SMB/NFS/network filesystem lock semantics;
- container/VM shared-volume aliases not covered by qualification;
- two intentionally distinct state directories controlling the same X account;
- pre-M7 binaries that ignore `AuthorityOwnerLock`;
- arbitrary path-alias attacks by hostile local code.

Path normalization is defense against ordinary same-path spelling/case aliases,
not a cryptographic filesystem identity proof.

---

## 18. Windows and multi-process qualification

M7 cannot infer Windows ownership behavior from single-process unit tests. The
qualification layer must run real sibling processes on actual Windows Server
2025 runners, matching the evidence discipline used by M6 Layer 6.

Qualification must cover at least:

```text
two simultaneous owner acquisitions -> exactly one owner
a live owner blocks a second process without timeout stealing
normal owner release -> successor can acquire
forced owner termination -> OS releases ownership
lock rendezvous file remains present -> successor still acquires when OS lock is free
same-process duplicate owner root -> denied
case/path-normalized same domain -> one owner
lock handle is not inherited into child process
owner crash before/after browser startup -> successor follows full hydration order
owner crash during EffectLedger/ReconciliationLedger I/O -> existing fail-closed semantics hold
stale client instance after takeover -> rejected
local IPC endpoint is not remotely reachable
IPC executable deserialization is absent
```

Linux/POSIX multi-process tests should run as well, but Windows evidence is a
first-class completion gate because WireAgent's current qualified local target is
Windows.

No stronger OS-lock portability statement is made than the environments and
primitives actually tested.

---

## 19. Frozen M7 invariants

1. Exactly one M7-aware production authority owner exists per canonical authority
   domain at a time.
2. The owner domain is based on canonical local `state_dir` identity.
3. Lock-file existence, PID, mtime, age, or heartbeat never grant or revoke
   ownership.
4. Production ownership uses a non-expiring OS-held lock.
5. An alive/hung owner is never automatically timed out and replaced.
6. Takeover requires prior owner release or process death/termination.
7. `AuthorityOwnerLock` is acquired before authoritative recovery hydration and
   before browser startup.
8. `AuthorityOwnerLock` is released last after supported mutation/browser
   authority is quiesced.
9. Same-process sibling authority roots for one domain are denied.
10. The OS lock handle is non-inheritable by child processes.
11. Every owner acquisition creates a fresh random `authority_instance_id`.
12. `authority_instance_id` is not durable effect/reconciliation lineage.
13. A released `AuthoritySession` is permanently dead.
14. A successor never reconstructs old ConfirmationState, ApprovalGrant,
    EffectPermit, M5 claim, browser lease, request cache, or reconciliation
    authority.
15. All production browser reads and writes execute in the owner process.
16. Attach mode remains diagnostic, not production takeover authority.
17. A surviving orphan browser after owner death is not reattached as production
    authority.
18. One owner-side Dispatcher remains the supported capability authority root.
19. One owner-side ConfirmationState serves all clients.
20. M6 epoch advancement invalidates pending confirmations from every client of
    that owner.
21. Owner restart invalidates all old pending confirmation authority naturally.
22. One owner-side ReconciliationCoordinator serves the authority domain.
23. Standalone recovery may write only after acquiring the same owner lock.
24. An active production owner forces external recovery clients to route through
    the owner or fail busy.
25. EffectLedger and ReconciliationLedger remain single-writer in the supported
    M7 topology; M7 does not claim arbitrary external multi-writer safety.
26. M5/M6 durable state machines and immutable history semantics are unchanged.
27. Process death is never proof of `NO_EFFECT`.
28. Owner takeover never clears RecoveryGuard uncertainty.
29. Transport request ids are not effect ids and carry no durable replay truth.
30. Owner-side request dedupe is ephemeral and cannot clear/authorize recovery.
31. Same request id with changed canonical request is rejected.
32. Stale `authority_instance_id` is rejected before owner execution.
33. Lost IPC response never authorizes automatic fresh-confirmation replay.
34. IPC exposes capabilities/operator workflow, not raw browser/ledger/gateway
    mutation surfaces.
35. IPC uses non-executable bounded serialization; pickle-equivalent executable
    deserialization is forbidden.
36. M7 opens no production TCP listener.
37. Kill does not transfer ownership or bypass M5/M6 recovery.
38. External kill hot-file observation retains the existing bounded M5 claim; M7
    does not claim instantaneous interruption of an in-progress OS/browser call.
39. A detected unexpected owner-lock loss makes the current authority session
    terminal and mutation fail closed.
40. M7 makes no cross-machine, distributed exactly-once, hostile-local-code, or
    network-filesystem claim.
41. Different configured state roots are distinct M7 authority domains even if
    they later target the same remote account; no stronger global-account claim
    is implied.
42. Pre-M7 processes that ignore the owner protocol are outside the M7
    coordination claim.

---

## 20. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| M7-R1 | First process starts on free authority domain | Acquires owner lock before recovery/browser startup |
| M7-R2 | Two processes start concurrently on same domain | Exactly one owner; loser returns `authority_busy` before browser mutation authority exists |
| M7-R3 | Two Dispatcher authority roots in same Python process/path | Second denied by process-local owner registry |
| M7-R4 | `authority.lock` exists but OS lock is free | New owner may acquire; file existence is not authority |
| M7-R5 | Lock metadata PID/mtime appears stale while owner alive | No takeover |
| M7-R6 | Owner is paused/hung past arbitrary wall-clock duration | No automatic takeover |
| M7-R7 | Owner releases cleanly | Successor acquires new session/instance id |
| M7-R8 | Owner process is forcibly terminated | OS ownership releases; successor can acquire after process death |
| M7-R9 | Released AuthoritySession attempts operation | Denied fail-closed |
| M7-R10 | Child process is spawned while owner active | Child does not inherit usable ownership handle |
| M7-R11 | Same path with Windows case variation | Same authority domain / no second owner |
| M7-R12 | Corrupt EffectLedger at owner startup | Owner fails closed before browser/IPC readiness |
| M7-R13 | Corrupt/ambiguous ReconciliationLedger at startup | Same fail-closed behavior |
| M7-R14 | Browser startup fails after lock acquisition | No client readiness; cleanup may release only after no mutation-capable runtime remains |
| M7-R15 | Client connects to current owner | Handshake returns current instance id/version/capabilities |
| M7-R16 | Client sends stale owner instance id | Rejected before Dispatcher/recovery execution |
| M7-R17 | Client sends unknown protocol version/schema | Rejected before execution |
| M7-R18 | Same request id + same payload arrives concurrently | One owner-side execution; duplicate joins/waits or gets `request_in_progress` |
| M7-R19 | Same completed request id + same payload repeats | Same cached owner-instance response; no new execution |
| M7-R20 | Same request id + different payload | Protocol violation; no execution |
| M7-R21 | Owner restarts | Old request table is gone; old instance id rejected; no assumption about old effect |
| M7-R22 | Two clients obtain confirmation tokens | Both tokens live in the same owner ConfirmationState |
| M7-R23 | M6 reconciliation advances epoch | Pending tokens from all clients become stale |
| M7-R24 | Owner restarts with client holding old token | Old token has no reconstructed authority and old instance id is stale |
| M7-R25 | Client read and client write contend | Both route through owner and existing Dispatcher/browser coordination |
| M7-R26 | Second process attempts direct supported production browser start while owner active | Denied by authority ownership before browser startup |
| M7-R27 | Owner crashes leaving browser child alive | Successor does not attach to orphan as production authority |
| M7-R28 | Owner crashes before durable RESERVED | No old execution authority survives; future write requires fresh confirmation |
| M7-R29 | Owner crashes after RESERVED before terminal outcome | Successor RecoveryGuard blocks matching semantic replay |
| M7-R30 | Owner crashes after external effect but before evidence | Same unresolved/reconciliation path; process death is not no-effect proof |
| M7-R31 | Owner crashes after durable terminal EffectLedger outcome | Successor reconstructs settled history; no old grant/permit restored |
| M7-R32 | Owner crashes during ambiguous reconciliation append | Existing M6 ambiguity/re-durability rules remain authoritative |
| M7-R33 | Production owner active; second recovery process starts | Recovery process cannot write; routes via owner or fails `authority_busy` |
| M7-R34 | No production owner; standalone recovery starts | Acquires same owner lock before M6 authority and may reconcile normally |
| M7-R35 | Owner-side terminal reconciliation completes | Existing M6 publication ordering unchanged; all clients observe new owner state |
| M7-R36 | Client loses response to confirmed write; owner remains alive | Same request id does not start a second owner-side execution |
| M7-R37 | Client loses response and owner crashes | Successor does not replay automatically; durable M5/M6 truth governs next action |
| M7-R38 | Kill hot file trips while owner active | Existing authority boundaries block; ownership remains with same process |
| M7-R39 | Kill hot file trips while owner is hung in external call | No claim of instantaneous interruption; successor still cannot steal ownership |
| M7-R40 | Operator terminates hung owner | Successor can acquire only after OS lock release and then hydrates recovery before browser |
| M7-R41 | Controlled shutdown races new client request | Service drains/rejects; lock releases only after owner authority is quiesced |
| M7-R42 | IPC sends malformed/non-JSON/oversized frame | Rejected safely before dispatch; service remains available/fail-closed as appropriate |
| M7-R43 | IPC implementation review | No pickle/executable deserialization and no production TCP listener |
| M7-R44 | Actual Windows Server 2025 multi-process ownership run | Exactly one owner and crash-release/takeover behavior matches bounded claim |
| M7-R45 | POSIX multi-process ownership run | Same semantic contract under qualified POSIX primitive |
| M7-R46 | State directory resides on unsupported network/shared filesystem | M7 cross-process safety claim refused/not asserted |
| M7-R47 | Pre-M7 binary runs concurrently and ignores lock | Explicitly outside claim; tests/docs do not imply protection |

Additional mandatory regressions:

- M5 T1–T14 behavior remains unchanged under one owner;
- all M6 R1–R48 behavior remains unchanged under one owner;
- owner lock acquisition failure performs no ledger append, token issue, browser
  launch, or reconciliation mutation;
- stale owner instance cannot mint/consume confirmation through IPC;
- request-cache eviction cannot create external replay authority;
- journal content remains audit-only and cannot establish owner identity;
- owner instance id may appear in diagnostics/audit but never changes M5/M6
  lineage equality;
- shutdown/restart does not refund token bucket/dedupe state as a durable claim;
- offline reconciliation still requires explicit M6 operator confirmation;
- raw read-only diagnostics outside the owner are never used as mutation
  authority;
- tests use genuine sibling processes, not merely two objects in one interpreter.

---

## 21. Build order

```text
1. AuthorityOwnerLock + canonical domain + process-local owner registry
2. Dispatcher/recovery ownership integration + AuthoritySession lifecycle
3. owner instance identity + startup/shutdown/crash takeover qualification
4. local IPC transport + handshake + bounded read/health client path
5. capability writes + confirmation + reconciliation routing + ephemeral request table
6. multi-process crash/fault/response-loss qualification on POSIX
7. Windows Server 2025 multi-process lock/IPC/takeover qualification
```

Every implementation layer follows the existing project method:

```text
maintainer-first exhaustive review
-> explicit findings register
-> freeze exact candidate
-> CI
-> independent Codex/GitWire correctness review when actually available
-> reconcile findings
-> exact-head validation
-> pinned merge
```

If an independent general correctness-review integration is unavailable, use a
distinct recorded adversarial second pass. Do not relabel the maintainer's own
second reading as independent Codex verification.

### 21.1 Layer boundaries

**Layer 1** proves only ownership acquisition/release semantics. It does not start
a browser or expose IPC.

**Layer 2** makes ownership mandatory for supported Dispatcher and standalone
recovery runtime entrypoints, preserving existing M5/M6 inner behavior.

**Layer 3** qualifies owner lifecycle, stale-session denial, and crash takeover
before introducing a client protocol.

**Layer 4** adds local IPC for read/health surfaces only so framing, stale-instance,
size, serialization, and shutdown semantics can be qualified without external
mutation risk.

**Layer 5** routes write and reconciliation operations through the already-
qualified owner authority root. It does not duplicate WriteKernel or M6 state per
client.

**Layers 6–7** are qualification layers. Production changes occur only when real
multi-process/platform evidence falsifies a frozen assumption.

---

## 22. M7 completion criteria

M7 is complete only when:

```text
✓ exactly one supported owner process exists per canonical local state directory
✓ ownership is OS-held and non-expiring; no heartbeat/PID/mtime takeover exists
✓ losing processes fail before production browser or safety-write authority
✓ controlled release is last in owner shutdown
✓ forced process death permits OS-level takeover without stale-file deletion
✓ successor never restores old ephemeral confirmation/grant/permit/reconciliation authority
✓ all production browser reads/writes are owner-routed
✓ active-owner recovery is owner-routed; offline recovery acquires the same ownership boundary
✓ client stale-instance requests fail before execution
✓ local transport uses bounded non-executable serialization and no production TCP listener
✓ transport request dedupe is explicitly ephemeral/non-authoritative
✓ lost-response and owner-crash cases cannot become blind external replay
✓ M5 effect uncertainty and M6 reconciliation semantics remain unchanged
✓ genuine sibling-process crash/takeover tests exist
✓ actual Windows Server 2025 ownership/IPC behavior is qualified and claim-bounded
✓ documentation states the hostile-same-user, network-filesystem, mixed-version,
  multi-state-root, cross-machine, and distributed-exactly-once exclusions
```

The bounded M7 claim is:

> On one qualified local machine and one canonical WireAgent state directory,
> cooperating M7-aware WireAgent processes admit at most one supported production
> authority owner at a time. Other processes interact through the owner's local
> capability/reconciliation boundary or fail busy. After owner death, a successor
> rebuilds authority from durable M5/M6 truth and never resumes old ephemeral
> execution authority.

That claim deliberately stops short of distributed exactly-once execution,
automatic hot failover, hostile-local-process isolation, network-filesystem
coordination, or global uniqueness across separate state roots.
