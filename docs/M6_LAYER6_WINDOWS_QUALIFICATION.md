# M6 Layer 6 — Windows Durability Qualification

```text
Status: FIRST-PASS FINDINGS FROZEN — IMPLEMENTATION / PLATFORM EVIDENCE PENDING
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

## Planned evidence

A dedicated Windows qualification suite will be added and run under CPython
3.11 and 3.12. It will be intentionally ledger-focused rather than claiming the
entire browser runtime is Windows-qualified. The normal Ubuntu CI remains the
full regression/lint/type gate.

Planned Windows assertions include:

1. both ledgers create nested state paths and survive a fresh interpreter;
2. real `os.fsync()` succeeds on the writable descriptors WireAgent uses;
3. injected file-flush failure cannot become success; exact retry re-durabilizes
   the same fact without duplication;
4. startup ReconciliationLedger authority performs its writable-handle flush;
5. case-variant same-file paths share one EffectLedger lock / one M6 path state;
6. terminal-newline and UTF-8 corruption fail closed;
7. a durable reconciliation survives process restart and yields the same clear
   composite recovery truth;
8. the evidence report records runner OS, Python version, filesystem-facing
   behavior, and the Windows parent-directory claim ceiling.

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
