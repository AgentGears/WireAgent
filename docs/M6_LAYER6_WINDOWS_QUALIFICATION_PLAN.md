# M6 Layer 6 Candidate Validation Plan

This short companion records the execution gate for the Layer-6 candidate. The
first-pass findings themselves are frozen in `M6_LAYER6_WINDOWS_QUALIFICATION.md`.

Candidate validation is not complete until all of the following are true:

- full Ubuntu Python 3.11/3.12 suite passes;
- Ruff and mypy pass on the normal CI jobs;
- Windows Python 3.11/3.12 safety-ledger qualification jobs pass on actual
  `windows-latest` runners;
- Windows logs record `os.name=nt`, `sys.platform=win32`, the runner platform,
  and Python version;
- the Windows-focused suite exercises both ledgers, exact re-durability,
  ambiguity handling, startup re-durability, corruption/torn-tail handling,
  normalized path identity, and fresh-process restart projection;
- the final documentation states the parent-directory and storage-stack claim
  ceiling explicitly;
- a second independent/adversarial review occurs only after an exact green head;
- any finding from that second pass is reconciled and exact-head CI rerun before
  merge.
