# M6 Layer 6 — Windows Durability Qualification

```text
Status: PLATFORM EVIDENCE GREEN — CLOSE-OUT REVIEW / EXACT-HEAD REVALIDATION PENDING
Baseline: e1e3eeb679cf54b5707c88b0e82e3db4a04d9319
Scope: M6_DESIGN.md §17, R46, invariant 34, build-order layer 6
```

This layer qualifies the two safety ledgers on an actual GitHub-hosted Windows
runner. It does not broaden M6 into a cross-process protocol, a universal
filesystem guarantee, or a replay-policy promotion.

## Qualification boundary

The frozen M6 requirement is deliberately bounded. Windows evidence must cover:

- nested state-directory and file creation;
- append plus file-descriptor flush for both `EffectLedger` and
  `ReconciliationLedger`;
- exact-fact re-durability using a writable descriptor after an injected
  ambiguous file-flush failure;
- the reconciliation ambiguity latch across same-path objects;
- startup re-durability of a surviving reconciliation file before it can clear
  recovery;
- torn/corrupt tail fail-closed behavior;
- restart projection from durable files;
- Windows path/case identity for process-local safety domains;
- parent-directory behavior only to the extent exposed by the Python/Windows
  API used by WireAgent.

The target claim is therefore: **the tested CPython/Windows runner successfully
executes the ledger write/fsync/re-durability/restart protocol and fails closed
on the tested ambiguity/corruption cases.** It is not a claim that Python's file
flush bypasses every hardware/controller cache, that directory entries have a
portable Windows fsync primitive, or that network filesystems share local-disk
semantics.

## Maintainer-first exhaustive findings register

The findings below were frozen before any independent/adversarial second pass.

### L6-F01 — actual Windows evidence is absent

Current CI runs only `ubuntu-latest` on Python 3.11/3.12. Existing Windows
branches in tests are simulations (`os.name` monkeypatching) and therefore do
not satisfy R46. Layer 6 requires a real `windows-latest` qualification job.

### L6-F02 — EffectLedger's same-path lock identity is not Windows-normalized

`EffectLedger` documents that all instances targeting the same normalized path
share one writer lock, but `_lock_for_path()` keys the registry by
`str(path.resolve(strict=False))` without `os.path.normcase()`. The M6
ReconciliationLedger, publication fence, CommitGateway lifecycle registry, and
coordinator path checks use normalized case identity. On a case-insensitive
Windows filesystem, two case variants can therefore name one file while
receiving different EffectLedger writer locks. This falsifies the documented
same-process serialization claim on the target platform.

Required correction: normalize EffectLedger's registry key with the same
`os.path.normcase(str(path.resolve(strict=False)))` rule and qualify it on real
Windows.

### L6-F03 — EffectLedger can bless a complete JSON torn tail

`EffectLedger.read_records()` uses `read_text().splitlines()` and accepts a
non-empty final JSON record that lacks the append protocol's terminal newline.
A later `O_APPEND` write would concatenate the next record to that same physical
line. `ReconciliationLedger` already rejects this state as a torn tail.

Required correction: a non-empty EffectLedger file without the terminal newline
must fail closed before it can become recovery authority or accept another
append.

### L6-F04 — EffectLedger's non-UTF-8 corruption does not use the ledger error contract

`Path.read_text(encoding="utf-8")` can raise `UnicodeError`, but
`EffectLedger.read_records()` currently wraps only `OSError`. Recovery still
fails because the exception propagates, but the safety-ledger API contract and
its callers should receive `EffectLedgerCorruptError`, matching the
ReconciliationLedger corruption boundary.

Required correction: classify invalid UTF-8 explicitly as safety-ledger
corruption and cover it directly.

### L6-F05 — parent-directory durability claim must remain intentionally weaker on Windows

Both ledgers intentionally no-op `_fsync_directory()` when `os.name == "nt"`.
That is a claim boundary, not something Layer 6 may silently reinterpret as a
successful directory fsync. Windows qualification may establish file-descriptor
flush behavior and successful nested path creation; it must continue to state
that WireAgent has no portable Python-level parent-directory flush guarantee on
Windows.

No stronger production mechanism is introduced in Layer 6 unless platform
evidence requires one.

## Implemented corrections and qualification surface

The Layer-6 candidate makes only the production changes falsified by the first
pass:

- `EffectLedger` same-path lock identity now uses the same
  `os.path.normcase(str(path.resolve(strict=False)))` rule as the M6 safety
  domain registries;
- a non-empty EffectLedger file without the canonical terminal newline is
  corruption rather than authoritative complete history;
- invalid UTF-8 is classified as `EffectLedgerCorruptError`.

CI now retains the normal full Ubuntu Python 3.11/3.12 test + Ruff + mypy gate
and adds a dedicated `windows-latest` Python 3.11/3.12 safety-ledger matrix. The
Windows matrix is deliberately focused on both ledgers and the frozen R46
contract rather than claiming the browser runtime as a whole is Windows
qualified.

The platform suite directly or through the existing durability regressions
covers:

1. nested state-directory/file creation for both ledgers;
2. actual Windows `os.fsync()` on writable file descriptors used by the ledger
   durability protocol;
3. injected append/fsync ambiguity followed by exact-fact re-durability without
   duplicate authority;
4. shared ReconciliationLedger ambiguity state across same-path objects;
5. startup writable-handle re-durability before a surviving reconciliation row
   may clear recovery;
6. case-variant same-file identity for both EffectLedger and
   ReconciliationLedger process-local domains;
7. terminal-newline, invalid-UTF-8, malformed-tail, lineage, and schema
   corruption fail-closed behavior;
8. a durable reconciliation surviving a fresh Python process and yielding the
   same clear composite recovery result;
9. an explicit assertion that the parent-directory fsync hook is a no-op on the
   tested Windows implementation rather than evidence of a directory flush.

## Platform evidence

The first platform run, CI #454, proved the job was executing on a real Windows
host but found one diagnostic-compatibility regression: an existing corrupt-tail
test expected the phrase `not valid JSON` while the new stricter framing check
reported `torn tail`. The safety behavior was already fail-closed. The message
was made backward-compatible without weakening the new terminal-newline rule,
and the matrix was changed to `fail-fast: false` so both Python versions retain
diagnostic evidence independently.

Candidate `71b365d18d6e6d07462e758e42f176f71a4e9c7b` then passed CI #457:

```text
Windows host: Microsoft Windows Server 2025, 10.0.26100 Datacenter
Runner image: windows-2025-vs2026, image version 20260922.246.2

CPython 3.11.9 / MSC v.1938 x64
  os.name      = nt
  sys.platform = win32
  focused safety-ledger suite: 147 passed

CPython 3.12.10 / MSC v.1943 x64
  os.name      = nt
  sys.platform = win32
  focused safety-ledger suite: 147 passed

Ubuntu full regression gate, Python 3.11.16
  936 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 84 source files

Ubuntu full regression gate, Python 3.12.14
  936 passed, 6 Windows-only skipped
  Ruff clean
  mypy clean across 84 source files
```

This is actual target-platform evidence for the tested GitHub-hosted Windows
Server 2025 / CPython combinations. Close-out documentation changes after this
candidate require another exact-head four-job CI run before review/merge.

## Qualified claim ceiling

Layer 6 supports the following bounded statement and no stronger one:

> On the tested GitHub-hosted Windows Server 2025 runners, CPython 3.11.9 and
> 3.12.10 successfully execute WireAgent's file-handle append/fsync,
> exact-fact re-durability, startup reconciliation re-durability, normalized
> same-path coordination, corruption fail-closed, and fresh-process recovery
> protocol for the two safety ledgers.

The evidence does **not** establish:

- a portable parent-directory fsync/FlushFileBuffers guarantee through the
  Python APIs used here;
- bypass of all OS, controller, device, or storage-stack caches;
- equivalent semantics on SMB/NFS/network-backed or otherwise different
  filesystems;
- cross-process writer linearizability;
- browser-runtime Windows qualification beyond the safety-ledger scope tested;
- distributed exactly-once behavior.

## Review discipline

Layer 6 follows `M6_DESIGN.md` §21:

```text
maintainer-first exhaustive review
→ explicit findings register (this document)
→ freeze exact candidate
→ CI including actual Windows qualification
→ independent Codex/GitWire review when available
→ reconcile findings
→ exact-head validation
→ pinned merge
```

If no independent review integration is exposed, the design-approved fallback is
a distinct recorded adversarial second pass after exact-head platform CI. That
fallback is not assumed approval and must be recorded explicitly.
