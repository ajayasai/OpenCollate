# Source-bound upgrade validation

This record describes the development upgrade in PR #6, after v0.3.0. These are
finite public regression results, not a comparison with licensed commercial tools.

## Source and solver correctness evidence

The dedicated sequential suite has 185 passing tests with Icarus installed. Of
these, 62 native-slang arithmetic oracles evaluate 15,872 signed/unsigned input
assignments, comparing both SMT lowering and the separate Python interpreter with
native constant evaluation. Four further tests replay counterexample traces in
Icarus Verilog, independently of Z3 and the Python trace interpreter.

The source-file benchmark has 12 expected outcomes. It includes 256-bit,
16-stage pipelines (4,096 state bits), inversion/latency/enable mutations,
bounded-only checks, guard activation, and an intentionally late bug that first
fails at sample 10. A depth-6 run of that design is bounded, never proven.
Unsupported asynchronous reset is rejected. These structured examples do not
establish capacity for arbitrary 4,096-bit state machines or production SoCs.

```console
pytest -q tests/test_sequential.py tests/test_sequential_oracles.py
python benchmarks/sequential.py --repeat 3 --json-output sequential-results.json
```

## Review-driven corrections

Windows CI exposed differences between temporary path spellings used by Python
and native frontends. The upstream fixture benchmark now recognizes original,
canonical and relative roots, normalizing separators only within source paths.
Line/column information, HDL expressions and unrelated paths remain intact.

Review also found that the guard's status output could overwrite an input file.
Status destinations now must be new per-run files, checked before child execution.
Existing files, hard-link aliases and symlinks are rejected, as are collisions
with declared child outputs and generated output/cache directories. Atomic
exclusive publication preserves a file that appears after preflight. Reusing an
old status filename is deliberately rejected. All 55 guard/status regression
tests pass, including the original configuration-overwrite reproducer.

## Verified runs

The initial source validation is [run 34023460113](https://github.com/ajayasai/OpenCollate/actions/runs/34023460113).
The reviewed safety correction is [run 34024667430](https://github.com/ajayasai/OpenCollate/actions/runs/34024667430).
The latter ran 1,033 tests successfully with 2 browser tests delegated to the
separate Chromium job, passed optimized Python, and measured 86.53% coverage with
branches included. Local Linux validation passed 1,029 tests with 6 skips: the
two browser tests and four Icarus tests run in their dedicated remote jobs.

The mutation corpus remains 34 exact mutant detections with 34 clean controls;
the 8-case combinational symbolic corpus and 4-case public conformance corpus
also passed during source validation. The pinned upstream corpus contains only
two cell-interface controls and six source-level mutations.

The retained `incremental.json` artifact from the initial GitHub validation reports
a 5.71x warm-check speedup against the same uncached OpenCollate path on synthetic
512-cell Liberty/LEF collateral (26,891,279 source bytes). Three-sample medians were
4.6265 seconds uncached, 5.2019 seconds cold-cache and 0.8108 seconds warm-cache.
Every report matched; a single-view mutation required one miss and one hit and
matched fresh analysis. These are host-specific, in-process measurements, not
commercial comparisons. The original prose timing/byte figures were transcribed
incorrectly; the retained raw artifact is authoritative and reproduced unchanged
in [the checked-in result](../benchmarks/results/source-bound-cache-2026-09-06.json).

Refer to the PR's final checks for the current cross-platform/security/browser
results; this document records measured runs rather than promising future checks.
See [source-bound semantics](source-bound-sequential.md) and
[cache/watchdog boundaries](incremental-and-guarded-checks.md) before using a result
as a CI gate. No safety certification or signoff qualification is claimed.
