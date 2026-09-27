# M7 — Cross-Process Authority & Ownership Boundary

```text
Status:   DESIGN CANDIDATE — MAINTAINER-FIRST + DISTINCT ADVERSARIAL PASS COMPLETE; NO IMPLEMENTATION YET
Base:     main f3268407167ad39f6be1aa01b4b8dc34cef59da7
Runtime:  M5 transaction boundary complete; M6 reconciliation/qualification Layers 1–7 complete
Scope:    same-machine, same-user, multiple cooperating M7-aware WireAgent processes sharing one canonical local state directory;
          exactly one production authority owner, local IPC clients, crash-safe takeover only after owner release/death
Change rule: preserve M5/M6 semantics; revise only when implementation evidence, fault injection,
             platform qualification, or an independently verified review finding falsifies an assumption
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

The first pass reviewed the exact M6-complete baseline `f3268407` before choosing
a mechanism. These are architectural facts at that baseline, not M5/M6 defects.

### M7-F01 — EffectLedger writer coordination is process-local

`EffectLedger` shares one normalized-path `threading.RLock` between same-process
instances and explicitly excludes independent external writers from its contract.
Its fsync/history rules are safety-critical, but the NDJSON format is not an
arbitrary concurrent multi-process writer protocol.

**Consequence:** M7 must not assume that two processes may safely write
`effects.ndjson` merely because both use `EffectLedger`.

### M7-F02 — ConfirmationState authority is process-local

`ConfirmationState` owns one in-memory epoch, pending-token store, monotonic clock
sampling domain, and consume fence. M6 intentionally does not persist or
reconstruct human confirmation authority.

**Consequence:** multiple process-local `ConfirmationState` objects would be
multiple human-authority roots. M7 keeps one canonical instance in the production
owner process.

### M7-F03 — M6 reconciliation ordering is process-local

`ReconciliationPublicationFence`, `ReconciliationCoordinator` protocol state,
CommitGateway lifecycle ownership, and committed-fact continuation are shared by
normalized paths only inside one process. M6 explicitly excludes independent
processes.

**Consequence:** a separate recovery process must not append reconciliation while
a production owner is active. Recovery routes through the owner or becomes the
exclusive owner after the prior owner exits.

### M7-F04 — browser coordination is process-local

`M5LeasedWriteBroker` attaches an async lease state to one SuperBrowser facade,
and `Dispatcher` serializes supported invocations because reads and writes share
that browser. A second process has neither synchronization domain.

**Consequence:** M7 cannot solve only ledger writes. Production browser reads and
writes must have the same process owner.

### M7-F05 — Dispatcher is already the natural authority root

`Dispatcher` owns the browser session, kill switch, WriteKernel, EffectLedger,
RecoveryGuard, CommitGateway, live M5 stack, and M6 ReconciliationCoordinator.
It hydrates recovery before browser startup and serializes supported invocations.

**Consequence:** the least invasive design is one outer process-ownership boundary
around the existing authority root, not a rewrite of M5/M6 internals.

### M7-F06 — owned browser remains the production path

`SessionManager` defaults to launch/owned mode. Attach mode is explicitly a
non-production diagnostic path and requires opt-in.

**Consequence:** a successor starts a fresh owned session. It never silently
attaches to an orphaned browser from the dead owner and calls that production
authority.

### M7-F07 — M6 names cross-process coordination as the next forcing function

The M6 completion criteria explicitly state that independent runtime/recovery
processes sharing a state directory create the forcing function for a separate M7
authority/locking design.

### M7-F08 — time-based takeover is unsafe at this mutation seam

A browser click or X service mutation does not carry a WireAgent fencing token
that the remote system can reject. A heartbeat/TTL lease that permits takeover
while the old process is merely paused or hung can therefore create two live
mutation authorities if the old process wakes.

**Decision:** M7 does not use heartbeat expiry, PID age, lock-file mtime, or wall-
clock staleness to steal production authority. Availability yields to single-
owner safety.

---

## 2. Distinct adversarial second pass

The second pass was performed after the initial design candidate, separately from
the maintainer-first mechanism selection. It is not represented as independent
Codex/GitWire verification.

### M7-RV01 — instance-id checking alone leaves a shutdown admission race

A request can validate the current `authority_instance_id` while controlled
shutdown is simultaneously beginning. Without one owner-service lifecycle fence,
the request could be admitted after the service intends to quiesce, or the owner
lock could be released while work is still in flight.

**Correction:** M7 adds synchronized `AuthorityServiceLifecycle` state:

```text
STARTING -> READY -> DRAINING -> TERMINAL
```

Request admission and the `READY -> DRAINING` transition share one lifecycle
fence. Admission increments an active-request count under that fence. Shutdown
moves to `DRAINING`, rejects new admissions, waits for admitted work to leave the
owner execution domain, retires browser/IPC authority, marks the session terminal,
and releases the OS owner lock last. A hung admitted operation prevents clean
release; the operator must terminate the owner process for takeover.

### M7-RV02 — mutating client retry across owner replacement needed an explicit law

An old client can reconnect after owner death, learn the successor's new instance
id, and mechanically resend an old mutating payload. Stale-instance rejection
alone does not prevent that client behavior.

**Correction:** the M7 client contract forbids automatic mutation/reconciliation
retry across `authority_instance_id` change. An in-flight mutating request whose
owner disappears is returned to the caller as transport/owner outcome uncertainty.
The successor's M5/M6 durable truth is inspected before any genuinely fresh human-
approved action is created. Read-only operations may be retried after a fresh
handshake because they do not cross the remote mutation seam.

### M7-RV03 — an OS lock primitive must have stronger semantics than “a file lock”

Some lock APIs have surprising process/descriptor semantics. M7 cannot accept a
primitive whose ownership can be lost when an unrelated descriptor closes or
silently inherited into a child process.

**Correction:** the platform adapter must qualify a dedicated, non-inheritable
owner handle whose lock lifetime is tied to that handle/process and whose release
behavior is not affected by unrelated descriptors. The handle remains private to
`AuthorityOwnerLock`. Explicit release marks the `AuthoritySession` terminal
before closing the OS ownership handle. A primitive that cannot establish these
properties is unsupported.

### M7-RV04 — request-cache claims exceeded a bounded cache

A bounded ephemeral request table cannot promise that every request id remains
recognizable for the whole owner lifetime after eviction.

**Correction:** transport dedupe is explicitly bounded by retention. While a
request entry is retained, same-id/same-canonical-request joins or returns the
same result and same-id/different-request is rejected. After eviction there is no
transport-level exactly-once claim; M5/M6 confirmation/effect/recovery authority
remains the safety boundary. Request-cache eviction can never create permission
to replay an external mutation.

### M7-RV05 — client disconnect must not become mutation cancellation

If the socket/task lifetime owns the Dispatcher task, a client disconnect can
cancel work after mutation authority has crossed, creating unnecessary ambiguity
or inconsistent transport semantics.

**Correction:** once a mutating or terminal-reconciliation request is admitted,
its owner-side task is independent of the client connection lifetime. Client
timeout/disconnect means “response unavailable,” not “operation cancelled.” The
owner drives the admitted operation to the existing safe M5/M6 terminal boundary
unless kill/failure semantics inside that boundary decide otherwise. M7 exposes no
generic remote cancel for an in-flight mutation.

### M7-RV06 — local IPC has its own stale endpoint problem

POSIX Unix-domain socket pathnames can survive an owner crash even though the
socket is no longer live.

**Correction:** endpoint cleanup/bind happens only after `AuthorityOwnerLock` is
held. The exclusive owner may remove a stale local IPC rendezvous path before
binding. IPC pathname existence is never ownership authority. Windows named-pipe
or equivalent endpoint lifetime is qualified separately.

### M7-RV07 — local-output capabilities need separate IPC path semantics

`download_image` is not a remote X mutation but writes to local storage. Routing
it from a client process through an owner changes which process/CWD owns the
output path.

**Correction:** M7 Layer 4 initially qualifies pure read/health operations only.
`download_image` is withheld from IPC until a bounded owner-side output contract
is specified (for example an owner-defined output root plus returned artifact
metadata). It must not inherit arbitrary client filesystem paths merely because
it is classified as a read capability.

---

## 3. Architecture decision — one authority process, many local clients

The supported production topology is:

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
        | ServiceLifecycle      |
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

Only the active owner process may perform supported production browser operations
or safety-ledger mutations. Other M7-aware processes are local clients. If the
local IPC service is unavailable/disabled, a second process fails
`authority_busy`; it does not create a second authority root.

This is a **single-writer / single-browser-owner architecture**, not consensus.

### 3.1 Why centralize instead of adding cross-process locks everywhere

M5/M6 already have a coherent authority root and carefully ordered internal
locks. Adding unrelated OS locks to EffectLedger, ReconciliationLedger,
ConfirmationState, RecoveryGuard, CommitGateway, browser leases, and operator
workflow would create new lock orders and still could not fence the remote
browser/X mutation seam.

M7 therefore adds one outer law:

```text
AuthorityOwnerLock — held for the entire production authority lifetime
    -> existing Dispatcher / M5 / M6 lock orders remain unchanged
```

Within this supported topology, one process owner means the existing process-
local M5/M6 synchronization remains canonical inner synchronization.

---

## 4. Scope and non-goals

### 4.1 In scope

M7 defines and qualifies:

1. one cross-process authority domain per canonical local WireAgent state dir;
2. a non-expiring OS-held `AuthorityOwnerLock` for that domain;
3. one process-local terminal `AuthoritySession` per acquisition;
4. one synchronized `AuthorityServiceLifecycle` for admission/drain/termination;
5. one fresh random `authority_instance_id` per successful acquisition;
6. owner acquisition before authoritative recovery or browser startup;
7. owner release only after supported browser/mutation authority is quiesced;
8. local IPC client access without raw browser/ledger/gateway surfaces;
9. stale-client and stale-owner-instance rejection;
10. bounded ephemeral owner-side transport request dedupe;
11. owner-side human confirmation shared across clients;
12. owner-side M6 reconciliation shared across clients;
13. crash/restart/takeover preserving M5/M6 durable uncertainty;
14. real sibling-process qualification, especially Windows Server 2025.

### 4.2 Out of scope

M7 does **not** provide:

- multiple simultaneous production owners;
- heartbeat/TTL automatic failover while an owner process is alive;
- multi-machine coordination, leader election, quorum, or consensus;
- distributed exactly-once execution;
- arbitrary multi-writer EffectLedger/ReconciliationLedger semantics;
- remote/network IPC exposure;
- multi-user RBAC or hostile same-user process isolation;
- Administrator/root protection from file/process/browser bypass;
- network-filesystem lock/durability semantics;
- durable human confirmation, ApprovalGrant, EffectPermit, browser lease, or
  request-cache state;
- replay-policy changes from M6;
- reconciliation correction/supersession;
- durable cross-restart token-bucket or semantic-dedupe state;
- account-global uniqueness across intentionally different state roots;
- compatibility safety against an older pre-M7 binary that ignores ownership.

---

## 5. Constitutional distinctions

M7 preserves:

```text
lock-file existence       != authority ownership
PID                        != authority ownership
heartbeat freshness        != authority ownership
client connection          != execution authority
authority instance id      != durable effect identity
request id                 != effect id
transport dedupe           != semantic/effect replay safety
IPC response success       != external effect confirmation
client disconnect          != mutation cancellation
process death              != proof of NO_EFFECT
owner takeover             != old-attempt continuation
browser process existence  != WireAgent browser authority
attach mode                != production ownership
recovery clear             != permission to execute
M7 owner lock              != M5/M6 policy approval
```

Existing project laws remain unchanged:

- **Cognition != authority.**
- **Execution != external effect.**
- **Evidence != claim.**
- **UNKNOWN != NO_EFFECT.**
- **Recovery clear != execution permission.**

---

## 6. Authority domain and owner lock

One M7 authority domain is identified by the canonical configured `state_dir`.
The implementation normalizes the local path before registry lookup and lock
acquisition using the same case-normalization posture already required by the
Windows safety-ledger work.

Conceptually:

```text
authority_domain = canonical_local_path(WebWireConfig.state_dir)
authority_lock   = authority_domain / "authority.lock"
```

The lock file is a stable rendezvous object only. Its existence, content, PID,
mtime, or age is never authority.

### 6.1 `AuthorityOwnerLock`

Required contract:

- exclusive for one canonical authority domain;
- supports non-blocking acquisition for deterministic `authority_busy`;
- ownership is held continuously for the authority lifetime;
- OS releases ownership on owner process death/owner-handle close;
- ownership is **not** time-expiring;
- dedicated owner handle is non-inheritable;
- unrelated descriptors cannot release ownership;
- owner handle is private and not serialized/exposed;
- one process-local normalized-domain registry also denies accidental sibling
  authority roots in one interpreter;
- acquisition/release errors fail closed;
- rendezvous file is not deleted during normal transfer.

The concrete Windows/POSIX primitive is an implementation choice that must be
qualified against this contract. A primitive that cannot satisfy it is not used.

### 6.2 No stale-lock-file cleanup

After a crash, `authority.lock` may remain. A successor opens the same rendezvous
and asks the qualified OS primitive whether exclusive ownership is available. It
never deletes a supposedly stale lock file, trusts a PID, or compares mtime.

### 6.3 `AuthoritySession`

Successful acquisition creates:

```text
AuthoritySession
  authority_domain
  authority_instance_id   # random 256-bit runtime identity
  acquired_at             # wall-clock provenance only
  active                  # process-local terminal lifecycle
  owner_handle            # private
```

`authority_instance_id` is runtime protocol identity/provenance. It is **not** an
EffectLedger/ReconciliationLedger lineage field and is not persisted as execution
authority.

Explicit release marks the session terminal before closing the owner handle. A
terminal session can never become active again. Reacquisition creates a new
session and performs complete startup/hydration.

### 6.4 No durable owner generation in M7

M7 forbids live-owner stealing. A next owner acquires only after clean release or
process death. Old M5/M6 ephemeral authority is never reconstructed across that
boundary.

A fresh random instance id is therefore sufficient for stale-client protocol
rejection without introducing a third durable safety history and its own crash
protocol. If a future design permits takeover while an old owner may remain live,
it requires a mutation seam that can reject stale fencing generations; the
current browser/X effect seam does not provide that property.

---

## 7. Authority service lifecycle

Owner-service state is synchronized under one lifecycle fence:

```text
STARTING -> READY -> DRAINING -> TERMINAL
```

### 7.1 Request admission

A request may reserve an active owner request slot only if, under one lifecycle
critical section:

```text
session.active is true
service state == READY
authority_instance_id matches
request schema/version is valid
resource/backpressure admission succeeds
```

The same critical section increments the active-request count before the request
can run. The request slot is released after the owner-side operation reaches its
safe local terminal boundary.

### 7.2 Controlled drain

`READY -> DRAINING` is synchronized with request admission. Once draining begins:

- no new request slot can be admitted;
- already-admitted work continues under the existing Dispatcher/M5/M6 authority;
- shutdown waits for admitted work and the Dispatcher invocation domain to quiesce;
- a hung admitted mutation does not cause lock release by timeout.

If clean drain cannot complete, ownership remains held. Explicit process
termination is the fail-stop takeover path.

---

## 8. Startup and shutdown ordering

### 8.1 Owner startup

```text
canonicalize/create local state directory
-> reserve process-local authority-domain slot
-> acquire OS AuthorityOwnerLock
-> mint AuthoritySession + authority_instance_id
-> create AuthorityServiceLifecycle(STARTING)
-> construct canonical Dispatcher/M5/M6 authority root
-> hydrate/validate composite RecoveryGuard from both safety ledgers
-> if unavailable/corrupt: fail closed before browser startup
-> start fresh owned browser/session
-> install coherent M5/M6 live stack
-> create local IPC endpoint
-> transition STARTING -> READY
-> begin accepting client work
```

Ownership is acquired before authoritative recovery and before browser startup.
A losing process therefore cannot construct a second supported production
mutation path.

If startup fails before readiness, cleanup may transition terminal and release
ownership only after no mutation-capable runtime remains. A later owner still
performs normal M5/M6 hydration; startup failure never implies clean external
state.

### 8.2 Controlled shutdown

```text
READY -> DRAINING under lifecycle fence
-> stop new IPC admission
-> wait for admitted owner work / Dispatcher invocation to quiesce
-> advance/discard pending owner-side human confirmation authority for teardown
-> close owner-side operator sessions
-> retire/stop browser-capable live stack
-> close local IPC endpoint
-> AuthoritySession active -> terminal
-> release AuthorityOwnerLock LAST
```

If an in-flight operation cannot quiesce, clean shutdown does not release
ownership merely to improve availability.

The process may remain alive for diagnostics after release, but the terminal
session cannot authorize production work.

---

## 9. Takeover and hung-owner policy

M7 deliberately chooses **fail-stop takeover**, not time-based failover.

### 9.1 Clean takeover

After controlled release, a successor acquires ownership, gets a new instance id,
and rebuilds all ephemeral authority from scratch.

### 9.2 Crash takeover

After owner process death/forced termination, the qualified OS primitive releases
ownership. A successor performs the complete startup ordering and rebuilds truth
from durable M5/M6 histories.

Old confirmation tokens, ApprovalGrants, EffectPermits, M5 claims, browser leases,
request entries, and reconciliation authorities do not survive.

### 9.3 Hung owner

An alive but hung owner continues to hold ownership. Another process receives
`authority_busy`; no TTL, heartbeat, PID age, or mtime permits stealing.

Explicit operator recovery is:

```text
observe owner unhealthy
-> create kill hot file if useful
-> terminate hung owner if takeover is required
-> wait for OS ownership release
-> start successor normally
```

This is intentionally less available than a lease. It prevents a paused owner
from waking after timeout and crossing an unfenceable browser/X mutation seam.

### 9.4 Unexpected ownership-handle loss

`AuthorityOwnerLock` owns the dedicated handle and never exposes it. Explicit
release marks the local session terminal before closing it. If the platform
adapter detects unexpected loss/invalidity, the session becomes terminal,
service transitions to draining/terminal, new work fails closed, and the old
session is never silently reacquired.

Qualification must establish that unrelated descriptor close cannot release the
chosen ownership primitive. If that cannot be established, the primitive is
unsupported.

---

## 10. Local IPC boundary

M7 adds a local client/server boundary so additional processes do not construct
another Dispatcher/browser authority root.

### 10.1 Transport constraints

Production IPC is local-machine only:

- Windows: qualified local primitive such as a named pipe;
- POSIX: qualified local primitive such as a Unix-domain socket;
- no production TCP listener;
- endpoint permissions restricted to current user where exposed;
- no Python pickle or executable object deserialization;
- bounded strict non-executable framing such as UTF-8 JSON with explicit schemas,
  maximum frame/request sizes, and bounded queued/admitted work.

Transport can differ by platform; authority semantics above it do not.

### 10.2 Endpoint lifecycle

Endpoint creation occurs only after owner lock acquisition. On POSIX, a successor
that owns the authority lock may remove an unusable stale Unix-socket pathname
before binding. Socket-path existence is never ownership. Controlled shutdown
closes/unlinks the endpoint before releasing authority ownership.

### 10.3 Handshake

```text
AuthorityHello
  protocol_version
  authority_instance_id
  runtime_version
  supported_ipc_operations
  state: ready | draining | killed | recovery_unavailable
```

Handshake state is protocol/diagnostic information, not execution authority.

### 10.4 Request envelope

```text
AuthorityRequest
  protocol_version
  authority_instance_id
  request_id
  operation
  payload
```

Rules:

- instance id must match the live owner;
- request id is an opaque high-entropy client identifier;
- unknown version/operation/schema fails before Dispatcher/recovery invocation;
- stale instance fails `stale_authority_instance`;
- client never receives raw SuperBrowser, CommitGateway, EffectLedger,
  ReconciliationLedger, ConfirmationState, or ReconciliationAuthority objects.

### 10.5 Ephemeral owner request table

For a retained request entry:

```text
new id + canonical request          -> execute once
same id + same in-flight request    -> join/await or request_in_progress
same id + same completed request    -> return retained response
same id + different request         -> protocol violation; no execution
```

The table is process-local and bounded by count/time/resource policy. Its
retention window is a transport convenience, not a safety guarantee. After
entry eviction there is no transport-level exactly-once claim. M5/M6 remains the
external-effect authority.

### 10.6 Connection loss and cancellation

Once a **mutating or terminal-reconciliation** request is admitted, its owner-side
operation is detached from the client connection lifetime. Client disconnect or
RPC timeout means response uncertainty; it does not cancel the admitted mutation.
The owner drives the operation to its normal safe M5/M6 terminal boundary.

M7 defines no generic client cancel for an in-flight mutation. Any future
cancellation protocol must prove a precommit boundary or use existing kill/effect
semantics explicitly.

Read-only requests may be cancelled according to bounded read semantics when
that cannot interfere with an owned browser mutation/composer lease.

---

## 11. Browser ownership

1. Only the active authority owner starts/owns the production browser session.
2. All production browser reads **and** writes route through the owner.
3. Client processes do not directly navigate the production browser.
4. Existing Dispatcher serialization and M5 browser lease remain inner browser
   coordination.
5. Attach mode remains diagnostic, not production ownership/takeover authority.
6. After owner crash, a successor does not silently attach to an orphaned browser.
7. Successor launches/restores a fresh owned session and re-establishes live actor
   identity using existing whoami authority rules.

An orphan browser may still finish already-emitted network activity after owner
death. Browser-process survival is never proof of effect or no-effect; durable
M5/M6 evidence/recovery truth remains authoritative.

---

## 12. M5/M6 integration

M7 adds one outer lifetime boundary:

```text
AuthorityOwnerLock                 # M7, whole owner lifetime
  -> AuthorityServiceLifecycle
     -> Dispatcher invocation domain
        -> WriteKernel confirmation/policy shell
        -> RecoveryGuard
        -> M5 scoped authority
        -> CommitGateway
        -> EffectLedger
        -> browser effect/evidence

AuthorityOwnerLock                 # same owner
  -> AuthorityServiceLifecycle
     -> M6 ReconciliationCoordinator
        -> ReconciliationPublicationFence
        -> CommitGateway lifecycle fence
        -> ConfirmationState epoch
        -> ReconciliationLedger
        -> RecoveryGuard publication
```

M7 does not create a CommitGateway, ConfirmationState, browser lease, or
RecoveryGuard per client.

### 12.1 Ledgers remain single-writer in the supported topology

M7 does not redesign NDJSON ledgers as arbitrary multi-process writer formats.
The supported law is:

> A process without the active AuthoritySession cannot use a supported production
> path that writes EffectLedger or ReconciliationLedger.

Raw low-level class use by non-cooperating same-user code remains outside the
M7 threat model.

### 12.2 Existing durability semantics remain unchanged

Owner death during M5/M6 persistence uses current laws:

- torn/corrupt history fails closed;
- raw `RESERVED` after restart projects to unresolved unknown;
- `EFFECT_UNKNOWN` remains immutable historical truth;
- ambiguous reconciliation is non-authoritative until exact re-durability;
- old grant/permit/reconciliation authority is never reconstructed.

---

## 13. Human confirmation across clients

The owner has exactly one canonical `ConfirmationState` for its authority
instance. All clients share it indirectly:

```text
client A preview -> owner issues token T
client B preview -> owner issues token U
M6 reconciliation -> owner advances one confirmation epoch
T and U -> stale under the same owner state
```

Rules:

- clients hold opaque token strings/preview data only;
- canonical token authority remains private owner state;
- token authority is not bound to client process identity in the trusted same-
  user model;
- existing capability/intent/risk/single-use/monotonic-TTL rules remain
  authoritative;
- reconciliation advances the same epoch for every client;
- owner restart destroys all pending token authority;
- stale owner instance is rejected before an old token can reach current protocol
  execution.

Two concurrent requests carrying the same confirmation token are serialized by
the owner; the existing single-use consume boundary permits at most one normal
success path.

No durable cross-restart confirmation epoch is introduced because restart retains
no pending confirmation authority.

---

## 14. Reconciliation across processes

M6 terminal reconciliation remains explicit local human/operator authority. M7
changes only where that authority root lives.

### 14.1 Production owner active

Any external recovery/operator process routes through the owner IPC boundary or
fails `authority_busy`. It does not construct an independent write-capable
ReconciliationCoordinator for the same authority domain.

Owner-side reconciliation retains the existing M6 publication, lifecycle,
confirmation-epoch, durability, and RecoveryGuard ordering unchanged.

### 14.2 Owner absent

A standalone recovery program may acquire the same `AuthorityOwnerLock` and
become a temporary authority owner:

```text
acquire ownership
-> hydrate both safety ledgers
-> construct canonical M6 authority root
-> run explicit operator workflow
-> start owned browser only if bounded inspection requires it
-> drain/terminate owner lifecycle
-> release ownership last
```

If another owner is active, it cannot write reconciliation directly.

### 14.3 Operator sessions over IPC

IPC operator sessions are owner-side, ephemeral, bound to the current instance,
and closed by owner restart/drain. The M6 same-session explicit human
confirmation requirement for terminal reconciliation remains authoritative.

---

## 15. Kill semantics

The existing kill hot file remains the external same-state-directory emergency
mechanism. Kill is not ownership transfer.

- programmatic owner kill uses existing KillSwitch;
- an external local process may create the hot file;
- owner re-observes kill at existing M5 authority boundaries;
- kill does not release `AuthorityOwnerLock`;
- kill does not let a successor steal a live owner;
- kill does not rewrite/clear M5/M6 history;
- successor started while hot file remains begins mutation-blocked.

A hot-file trip cannot instantaneously interrupt an arbitrary OS/browser call
already in progress. If a hung owner must be replaced, the operator terminates
the owner; M5/M6 then governs any uncertain effect.

---

## 16. Client retry and lost-response semantics

Transport uncertainty must not become external-effect replay.

### 16.1 Same owner instance remains alive

While the request entry is retained, retrying the same request id/canonical
request joins or returns the same owner-side outcome instead of starting a second
copy.

If the entry was evicted, transport dedupe makes no guarantee. Mutating operations
remain protected by their deeper confirmation/M5/M6 semantics; eviction itself
is never permission to issue fresh authority.

### 16.2 Owner changes while a read-only request was in flight

After a fresh handshake, a client may retry a pure read operation according to
its normal bounded read semantics.

### 16.3 Owner changes while mutation/reconciliation outcome is unknown

The client library **must not automatically resend** the old mutation against the
new instance. It reports owner/transport outcome uncertainty. The successor then
uses M5/M6 durable truth:

```text
old owner unavailable / response unknown
-> fresh handshake with successor
-> inspect effect/recovery state
-> if durable uncertainty exists: remain blocked / reconcile
-> otherwise any later mutation is a genuinely fresh human-approved invocation
```

A new instance id is a restart fence, not authorization to repeat old intent.

`request_id` is never persisted into M5/M6 as a substitute for `effect_id`.

---

## 17. Crash and restart semantics

### 17.1 Crash before M5 durable reservation

No old ephemeral execution authority survives. Any later mutation requires fresh
normal confirmation.

### 17.2 Crash after durable `RESERVED`, before known terminal outcome

Successor hydration projects unresolved unknown. Matching semantic replay remains
blocked by RecoveryGuard.

### 17.3 Crash after external mutation, before terminal evidence/outcome

Same unresolved/reconciliation path. Process death is not `NO_EFFECT` evidence.

### 17.4 Crash after durable terminal M5 outcome

Successor reconstructs settled effect history. No old grant/permit is revived.

### 17.5 Crash during reconciliation

Existing M6 semantics apply: confirmation authority dies with the process;
visible-but-ambiguous reconciliation cannot clear recovery; surviving durable
reconciliation is re-durability-established before use.

### 17.6 Crash with surviving browser child

The child browser is not authority. Successor does not attach to it as production
session. Any already-emitted remote effect is represented only through M5/M6
history/evidence.

### 17.7 Machine reboot

OS ownership and all ephemeral authority disappear. New owner acquires normally
and rebuilds from durable M5/M6 state.

---

## 18. Local-output capability boundary

`download_image` is not an X mutation but has a local filesystem effect. M7 does
not silently treat client paths as owner-local paths.

Layer 4 IPC qualification initially includes pure read/health operations that
return bounded data and excludes `download_image`. Before `download_image` is
exposed over IPC, a separate bounded output contract must define:

- owner-approved output root;
- path normalization/traversal rules;
- artifact identity/metadata returned to the client;
- overwrite/collision semantics;
- cleanup/retention behavior;
- claim ceiling for local filesystem durability.

That work need not alter remote M5 effect semantics but must be explicit.

---

## 19. Security and trust boundary

M7 remains personal, local, single-user.

Supported failure model:

- multiple normal/cooperating M7-aware processes under the same local user;
- accidental concurrent startup;
- owner crash/forced termination;
- hung owner requiring explicit termination;
- stale client after owner restart;
- IPC disconnect/duplicate delivery/lost response;
- browser/runtime fault;
- existing ledger crash/corruption ambiguity handled by M5/M6.

Not protected against:

- malicious same-user code importing raw safety classes or driving another
  browser automation stack;
- Administrator/root bypass of files, process handles, or IPC ACLs;
- malicious safety-ledger editing;
- hostile kernel/filesystem behavior;
- remote multi-host races.

IPC must not expand the boundary with a network listener or executable
serialization.

---

## 20. Path, filesystem, and platform claim ceilings

The M7 ownership claim applies only to one canonical **local** state directory on
qualified platform/filesystem combinations.

Initial claim ceiling excludes:

- SMB/NFS/network/shared filesystem locking semantics;
- container/VM shared-volume aliases not covered by qualification;
- two intentionally distinct state dirs controlling the same remote actor;
- pre-M7 binaries that ignore the owner protocol;
- arbitrary path-alias attacks by hostile local code.

Known unsupported network/shared state should fail the production support gate
where detection is reliable. Where platform APIs cannot conclusively classify the
storage stack, runtime operation must not be documented as carrying the M7
qualified cross-process claim. An explicit diagnostic/unsupported override, if
one is added, must be visibly outside the production safety claim.

Path normalization is ordinary alias defense, not cryptographic filesystem
identity.

---

## 21. Windows and multi-process qualification

M7 cannot infer cross-process ownership from single-process tests. Qualification
must run genuine sibling processes on actual Windows Server 2025 runners, using
the same evidence discipline as M6 Layer 6.

Qualification covers at least:

```text
two simultaneous owner acquisitions -> exactly one owner
live owner blocks second process without timeout stealing
normal release -> successor can acquire
forced owner termination -> OS releases ownership
rendezvous file remains -> successor still acquires when OS lock is free
same-process duplicate authority root -> denied
case/path-normalized same domain -> one owner
owner handle is non-inheritable
unrelated descriptor close does not release owner lock
service drain races request admission -> one synchronized ordering
owner crash before/after browser startup -> successor performs full hydration
owner crash during EffectLedger/ReconciliationLedger I/O -> existing fail-closed semantics hold
stale client instance after takeover -> rejected
mutating client is not automatically replayed across instance change
admitted mutation survives client disconnect as owner-side work
stale POSIX socket path cleanup occurs only under authority ownership
production IPC is not remotely reachable
no executable deserialization exists
```

POSIX sibling-process qualification is also required. No stronger lock/IPC
portability statement is made than environments and primitives actually tested.

---

## 22. Frozen M7 invariants

1. Exactly one M7-aware supported production authority owner exists per canonical
   authority domain at a time.
2. Authority domain is based on canonical local `state_dir` identity.
3. Lock-file existence, PID, mtime, age, and heartbeat never grant/revoke
   ownership.
4. Production ownership uses a non-expiring qualified OS-held lock.
5. An alive/hung owner is never automatically timed out and replaced.
6. Takeover requires clean release or owner process death/termination.
7. Owner handle is dedicated, private, non-inheritable, and not releasable by
   unrelated descriptor close under the qualified primitive.
8. Owner lock is acquired before authoritative recovery and browser startup.
9. Owner lock is released last after supported owner work/browser authority is
   quiesced.
10. Request admission and transition to `DRAINING` share one lifecycle fence.
11. Clean shutdown never releases ownership while admitted mutating work remains.
12. Same-process sibling authority roots for one domain are denied.
13. Every acquisition creates a fresh random `authority_instance_id`.
14. Instance id is not durable effect/reconciliation lineage.
15. Released/terminal AuthoritySession can never authorize again.
16. Successor never reconstructs old ConfirmationState, ApprovalGrant,
    EffectPermit, M5 claim, browser lease, request cache, or reconciliation
    authority.
17. All production browser reads and writes execute in owner process.
18. Attach mode remains diagnostic, not production takeover authority.
19. Orphan browser after owner death is not reattached as production authority.
20. One owner-side Dispatcher remains the supported capability authority root.
21. One owner-side ConfirmationState serves all clients.
22. M6 epoch advancement invalidates pending confirmations from all clients.
23. Owner restart kills all pending confirmation authority naturally.
24. One owner-side ReconciliationCoordinator serves the authority domain.
25. Standalone recovery writes only after acquiring the same owner lock.
26. Active-owner recovery routes through owner or fails busy.
27. EffectLedger/ReconciliationLedger remain single-writer in supported topology.
28. M5/M6 state machines and immutable-history semantics are unchanged.
29. Process death is never proof of `NO_EFFECT`.
30. Owner takeover never clears RecoveryGuard uncertainty.
31. Request id is not effect id and carries no durable replay truth.
32. Request dedupe is bounded/ephemeral and cannot authorize or clear recovery.
33. Retained same request id with changed canonical request is rejected.
34. Stale instance id is rejected before owner execution.
35. Automatic mutating/reconciliation retry across owner-instance change is
    forbidden.
36. Read-only retry across owner-instance change may occur after fresh handshake.
37. Client disconnect/timeout does not cancel an admitted mutation/reconciliation.
38. IPC exposes bounded capabilities/operator workflow, not raw browser/ledger/
    gateway/authority objects.
39. IPC uses bounded non-executable serialization; pickle-equivalent executable
    deserialization is forbidden.
40. M7 opens no production TCP listener.
41. Endpoint-path existence is not authority; stale local endpoint cleanup occurs
    only while owner lock is held.
42. Kill does not transfer ownership or bypass M5/M6 recovery.
43. External hot-file kill retains existing bounded semantics; no instantaneous
    interruption claim is added.
44. Unexpected detected owner-lock loss terminalizes local authority; old session
    is never silently reacquired.
45. `download_image` is not exposed over IPC until local-output path semantics are
    explicitly qualified.
46. M7 makes no cross-machine, distributed exactly-once, hostile-local-code, or
    network-filesystem claim.
47. Different state roots are distinct authority domains even if they target the
    same remote actor.
48. Pre-M7 processes that ignore ownership are outside the M7 coordination claim.

---

## 23. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| M7-T1 | First process starts on free domain | Acquires owner lock before recovery/browser |
| M7-T2 | Two processes start concurrently | Exactly one owner; loser `authority_busy` before production browser/safety-write authority |
| M7-T3 | Two authority roots in same interpreter/path | Second denied |
| M7-T4 | `authority.lock` exists but OS lock free | New owner acquires; file existence irrelevant |
| M7-T5 | PID/mtime appears stale while owner live | No takeover |
| M7-T6 | Owner hung past arbitrary duration | No automatic takeover |
| M7-T7 | Owner releases cleanly | Successor acquires new session/instance |
| M7-T8 | Owner forcibly terminated | OS releases; successor may acquire after death |
| M7-T9 | Terminal AuthoritySession used | Denied fail-closed |
| M7-T10 | Child spawned while owner active | No inherited usable owner handle |
| M7-T11 | Unrelated descriptor to rendezvous closes | Qualified owner lock remains held |
| M7-T12 | Windows case/path alias | Same authority domain |
| M7-T13 | Corrupt EffectLedger on startup | Fail closed before browser/IPC ready |
| M7-T14 | Corrupt/ambiguous ReconciliationLedger | Same fail-closed behavior |
| M7-T15 | Browser startup fails after ownership | Never READY; release only after no mutation-capable runtime remains |
| M7-T16 | Admission races `READY -> DRAINING` | Request is either fully admitted before drain or rejected; no torn admission |
| M7-T17 | Clean shutdown with active request | Ownership held until request leaves safe owner boundary |
| M7-T18 | Active mutation hangs during shutdown | Clean release does not occur by timeout |
| M7-T19 | Client handshake current owner | Returns current instance/version/operations |
| M7-T20 | Client stale instance id | Rejected before Dispatcher/recovery execution |
| M7-T21 | Unknown protocol/schema/oversized frame | Rejected before execution |
| M7-T22 | Same retained request id + same payload concurrent | One execution; duplicate joins/in-progress |
| M7-T23 | Same retained completed id + same payload | Retained response; no new execution |
| M7-T24 | Same retained id + different payload | Protocol violation; no execution |
| M7-T25 | Request entry evicted | No transport exactly-once claim; eviction creates no mutation authority |
| M7-T26 | Owner restarts | Old table gone; old instance rejected |
| M7-T27 | Two clients obtain confirmation tokens | Both live in same owner ConfirmationState |
| M7-T28 | Two clients race same confirmation token | Existing single-use authority allows at most one success path |
| M7-T29 | M6 reconciliation advances epoch | Pending tokens from all clients stale |
| M7-T30 | Owner restarts while client holds token | Old token not reconstructed; old instance stale |
| M7-T31 | Client read/write contend | Both owner-routed through existing Dispatcher/browser coordination |
| M7-T32 | Second process attempts supported production browser start | Denied by ownership before browser startup |
| M7-T33 | Owner crash leaves browser child alive | Successor does not attach to orphan as production owner |
| M7-T34 | Crash before durable RESERVED | No old execution authority; fresh confirmation required |
| M7-T35 | Crash after RESERVED before terminal outcome | Successor RecoveryGuard blocks replay |
| M7-T36 | Crash after external effect before evidence | Same unresolved/reconciliation path |
| M7-T37 | Crash after durable terminal M5 outcome | Settled history recovers; no old grant/permit |
| M7-T38 | Crash during ambiguous reconciliation append | Existing M6 ambiguity/re-durability semantics hold |
| M7-T39 | Production owner active; external recovery starts | Cannot write independently; route through owner/fail busy |
| M7-T40 | No owner; standalone recovery starts | Acquires same owner lock before M6 authority |
| M7-T41 | Owner-side reconciliation completes | Existing M6 ordering unchanged; all clients share resulting owner state |
| M7-T42 | Client disconnects after mutating request admission | Owner task continues independently to safe M5/M6 terminal boundary |
| M7-T43 | Client retries same retained request after disconnect | Joins/returns same owner-side request |
| M7-T44 | Client loses mutation response and owner crashes | Successor does not auto-replay; M5/M6 truth governs next action |
| M7-T45 | Read response lost across owner restart | Fresh-handshake read retry allowed |
| M7-T46 | Kill hot file trips while owner active | Existing authority boundaries block; ownership unchanged |
| M7-T47 | Kill hot file trips while owner hung externally | No instantaneous-cancel/takeover claim |
| M7-T48 | Operator terminates hung owner | Successor acquires only after OS release, then hydrates before browser |
| M7-T49 | POSIX owner crashes leaving socket pathname | Successor removes/rebinds stale endpoint only after acquiring owner lock |
| M7-T50 | IPC malformed/non-JSON/executable payload attempt | Rejected safely; no executable deserialization |
| M7-T51 | IPC implementation inspection | No production TCP listener |
| M7-T52 | `download_image` requested before output contract | Not exposed/unsupported over M7 IPC |
| M7-T53 | Actual Windows Server 2025 sibling-process run | Ownership/crash-release/takeover matches bounded claim |
| M7-T54 | POSIX sibling-process run | Same semantic contract under qualified primitive |
| M7-T55 | Unsupported network/shared state | Production qualified claim refused/not asserted |
| M7-T56 | Pre-M7 process ignores owner lock | Explicitly outside claim; docs/tests do not imply protection |

Mandatory regressions:

- M5 T1–T14 behavior remains unchanged under one owner;
- M6 R1–R48 behavior remains unchanged under one owner;
- owner-lock acquisition failure issues no token, appends no safety fact, launches
  no browser, and commits no reconciliation;
- stale owner instance cannot mint/consume confirmation through IPC;
- request-cache eviction cannot grant external replay authority;
- journal remains audit-only and cannot establish owner identity;
- instance id may appear in diagnostics/audit but never changes M5/M6 lineage;
- process restart does not turn token-bucket/dedupe reset into a durable claim;
- offline reconciliation retains explicit M6 operator confirmation;
- raw diagnostics outside owner never become mutation authority;
- qualification uses genuine sibling processes, not two objects in one interpreter.

---

## 24. Build order

```text
1. AuthorityOwnerLock + canonical domain + process-local owner registry
2. AuthoritySession + AuthorityServiceLifecycle + Dispatcher/recovery ownership integration
3. owner instance identity + startup/shutdown/crash-takeover qualification
4. local IPC transport + handshake + bounded pure read/health path
5. write + confirmation + reconciliation routing + bounded request table
6. local-output (`download_image`) IPC contract, only if required for M7 completion/use
7. POSIX multi-process crash/fault/response-loss qualification
8. Windows Server 2025 multi-process lock/IPC/takeover qualification
```

Each implementation layer uses the established project method:

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
distinct recorded adversarial second pass. Never relabel the maintainer's own
second reading as independent Codex verification.

### 24.1 Layer boundaries

**Layer 1** proves only ownership acquisition/release semantics. No browser/IPC.

**Layer 2** makes ownership mandatory for supported Dispatcher and standalone
recovery entrypoints and freezes request-admission/drain ordering.

**Layer 3** qualifies lifecycle, stale-session denial, crash takeover, and no
heartbeat stealing before adding a client protocol.

**Layer 4** adds local IPC for pure read/health surfaces only so framing,
stale-instance, size/backpressure, disconnect, endpoint cleanup, and shutdown
semantics are qualified without remote mutation risk. `download_image` remains
withheld.

**Layer 5** routes write/confirmation/reconciliation through the existing owner
authority root. Mutating tasks are decoupled from client connection lifetime.

**Layer 6** is optional only in milestone ordering, not in correctness if
`download_image` is exposed over IPC; its output-path contract must be qualified
before exposure.

**Layers 7–8** are qualification layers. Production changes occur only when real
multi-process/platform evidence falsifies a frozen assumption.

---

## 25. M7 completion criteria

M7 is complete only when:

```text
✓ exactly one supported owner process exists per canonical qualified local state dir
✓ ownership is OS-held and non-expiring; no heartbeat/PID/mtime takeover exists
✓ chosen lock primitive has dedicated-handle/process-death semantics and no unrelated-descriptor release
✓ losing processes fail before production browser or safety-write authority
✓ request admission and owner drain have one synchronized ordering
✓ controlled release is last and never overtakes admitted mutation work
✓ forced process death permits OS-level takeover without stale-lock-file deletion
✓ successor never restores old ephemeral confirmation/grant/permit/reconciliation authority
✓ all production browser reads/writes are owner-routed
✓ active-owner recovery is owner-routed; offline recovery acquires same ownership boundary
✓ stale-instance requests fail before execution
✓ local transport uses bounded non-executable serialization and no production TCP listener
✓ stale local IPC rendezvous cleanup happens only under owner lock
✓ transport request dedupe is explicitly bounded/ephemeral/non-authoritative
✓ admitted mutation/reconciliation outlives client disconnect rather than being transport-cancelled
✓ client does not automatically replay mutation/reconciliation across owner-instance change
✓ lost-response and owner-crash cases fall back to M5/M6 durable truth
✓ `download_image` stays off IPC until its local-output contract is explicit
✓ M5 effect uncertainty and M6 reconciliation semantics remain unchanged
✓ genuine sibling-process crash/takeover tests exist
✓ actual Windows Server 2025 ownership/IPC behavior is qualified and claim-bounded
✓ hostile-same-user, network-filesystem, mixed-version, multi-state-root,
  cross-machine, and distributed-exactly-once exclusions remain explicit
```

The bounded M7 claim is:

> On one qualified local machine and one canonical WireAgent state directory,
> cooperating M7-aware processes admit at most one supported production authority
> owner at a time. Other processes interact through the owner's local bounded
> capability/reconciliation boundary or fail busy. After owner death, a successor
> rebuilds authority from durable M5/M6 truth and never resumes old ephemeral
> execution authority.

That claim deliberately stops short of automatic hot failover, distributed
exactly-once execution, hostile-local-process isolation, network-filesystem
coordination, or global uniqueness across separate state roots.
