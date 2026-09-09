# Manifest preprocessing validation — 9 September 2026

This records PR #16's opt-in, source-bound preprocessing upgrade against
`e0d8702bf270a456290afee3ed95d60682bd495c`. It is not a proprietary-tool comparison
or industrial qualification. See [semantics and limits](manifest-preprocessing.md).

## Final local regression

Linux x86-64 / Python 3.13.5 with Icarus installed:

- Full suite: **1,450 passed, 2 skipped**; optimized Python: **1,450 passed, 2 skipped**.
- The two skips are the existing Chromium tests, delegated to the browser workflow.
  All available Icarus oracles executed.
- Combined statement-and-branch coverage: **87.19%**, above the unchanged 85% gate;
  standalone branch coverage: **82.07%**. These are different metrics.
- **87** preprocessing and inventory-boundary tests pass both locally and in a
  separate exact-baseline checkout with only the complete patch applied. That
  checkout's imported implementation path is explicitly checked; it is not the
  editable working tree. This clean-checkout run covers the focused tests, not
  a second complete regression suite.
- Ruff formatting/lint, mypy (61 source files), Bandit, wheel/sdist builds and
  Twine pass. A freshly installed base-only wheel receiver has neither Z3 nor
  PySAT and checks the multi-file example certificate successfully.

The source benchmark has **12 explicit outcomes**, repeated three times: four
positive width tiers (1, 8, 64, 256), two source/build mutants, five specific
source/dependency rejections, and an inactive-header binding change. A positive
case must produce a checked certificate; a negative case must match its stated
reason, not merely throw an arbitrary exception. The independent Icarus oracle
compares all 256 byte values in both copy/invert builds, **512 simulator samples**.
The retained [initial raw benchmark](../benchmarks/results/preprocessing-2026-09-09.json)
records individual timings and dependency versions. It is not a speedup claim.

## GitHub source-validation runs and review corrections

[34308231834](https://github.com/ajayasai/OpenCollate/actions/runs/34308231834)
verified the initial exact source delta, then passed 1,445 tests, optimized Python,
87.17% combined coverage, static checks, packaging and the solver-free receiver.
The existing 34-pair mutation, 8-case symbolic, 12-case sequential, 12-case
certificate and 12-case controller suites passed in that run.

[34308916377](https://github.com/ajayasai/OpenCollate/actions/runs/34308916377)
validated a subsequent resource-handling correction: the lexical token budget is
shared before processing each file, and include rewrites use one chunk assembly
instead of repeatedly copying the whole source. That run passed 1,447 tests,
optimized Python, 87.18% combined coverage, and the repeated preprocessing corpus.

Review found that evidence schemas still allowed only 32 source records, despite
requests allowing 32 roots plus 128 headers. All three evidence schemas now
allow **160**: sequential receipts, certificates, and certificate-verification
results. New round-trip tests validate all four request/output schemas at 33 and
160 declared files, recheck stale-header rejection, and reject 161-record evidence
or more than 128 headers. Property/claim limits are unchanged. The permanent
preprocessing workflow runs these tests along with the native simulator oracle
and a separate solver-free wheel receiver. The PR checks contain the final
platform-specific, security and Chromium results.

## Exact scope

A proof is for one declared build configuration. All declared header bytes,
including unused or inactive headers, are bound. Ambiguous/missing includes are
rejected; ambient disk files do not participate. Relocation preserves logical
source binding. The loader is neither unrestricted preprocessing nor a sandbox;
native macro expansion still requires an external guard for a process deadline.
No multi-clock, asynchronous-reset, four-state, SVA, liveness, production-scale,
functional-safety or tapeout-signoff claim is made. Frontend/lowering/encoding and
the proof kernel remain trusted software, tested rather than mechanically proved.
