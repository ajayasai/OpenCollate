# Public benchmark and conformance suite

The public benchmark surface contains two complementary suites. Both use only
redistributable OpenCollate inputs and publish schema-validated deterministic
JSON artifacts.

## Parser and end-to-end conformance

`run.py` measures real parser and complete-project workflows:

- `report-diff` compares large diagnostic multisets containing duplicate
  fingerprints, additions, removals, semantic changes, unchanged findings,
  and adversarial input ordering. Its independent oracle checks every summary
  count and verifies that reversing both inputs leaves the serialized result
  unchanged.
- `systemrdl-import` compiles a public SystemRDL fixture and checks the selected
  top, nested register file, expanded register array, field access/reset data,
  widths, offsets, and absolute addresses against an exact independent oracle.
- `rtl-connectivity` elaborates a public SystemVerilog fixture and parses CSV
  intent. Its exact graph oracle requires one clean path, one forbidden-path
  witness, and one inconclusive result at a tainted unsupported-expression
  frontier.
- `uart-check` runs the complete public UART example through parsing,
  reconciliation, consistency checking, and JSON rendering. It verifies the
  report schema, inventory, exit status, and the three intentional findings.

Install OpenCollate, then run all four cases:

```console
python benchmarks/run.py --json-output benchmark-results.json
```

The nightly profile is reproducible from any checkout:

```console
python benchmarks/run.py \
  --repeat 5 \
  --warmup 1 \
  --diff-groups 2000 \
  --enforce-budgets \
  --json-output benchmark-results.json
```

Use repeated `--case` options to select cases. `--diff-groups`, `--repeat`,
and `--warmup` control scale and sampling. The four case-specific
`--*-budget-seconds` options set median regression ceilings; budgets are
informational unless `--enforce-budgets` is present.

## Semantic mutation recall and clean controls

`mutations.py` evaluates comparison semantics independently of parser fixture
coverage. Its checked-in manifest contains 34 paired cases across inventory,
interfaces, Boolean logic, package mapping, SDC, UPF, registers, DEF hierarchy,
and bounded static connectivity.

Every pair contains:

1. A clean control expected to produce no unwaived warning, error, or fatal
   diagnostic.
2. A minimally changed mutant with an explicit expected actionable diagnostic
   multiset.
3. A second execution with reversed observation order; the complete
   `EngineResult` must remain byte-equivalent after canonical serialization.

Run and enforce the full oracle:

```console
python benchmarks/mutations.py \
  --enforce-perfect \
  --json-output mutation-results.json
```

Selection is available through repeatable `--case` and `--family` options. Use
`--list` to print the immutable mutation manifest. The report publishes:

- target-code recall and false-negative count;
- exact detections and legitimate additional-diagnostic detection;
- inconclusive mutations;
- true-negative and false-positive clean controls;
- clean-control specificity;
- observation-order determinism;
- exact pair accuracy;
- per-family metrics;
- a manifest SHA-256 and full result SHA-256.

A passing result requires every mutant to match its exact oracle, every control
to remain clean, and every pair to be deterministic. The suite therefore fails
when a target is missed, an unrelated diagnostic appears, a clean control
fires, or input ordering changes the result.

## Reproducibility and interpretation

JSON is emitted with sorted keys, no timestamp, normalized repository-relative
paths, deterministic SHA-256 digests, and explicit conformance oracles. Reports
validate against the bundled Draft 2020-12 schemas:

- [`benchmark-results.schema.json`](benchmark-results.schema.json)
- [`mutation-results.schema.json`](mutation-results.schema.json)

Elapsed samples in `run.py` necessarily depend on the host, Python build,
filesystem, and system load. Its default 30-second ceilings are deliberately
generous guards against pathological regressions, not performance promises.
The mutation suite deliberately contains no timing field, so repeated results
are content-identical on equivalent OpenCollate versions.

These results compare OpenCollate releases and establish behavior only for the
published oracle corpus. They do not establish universal feature or performance
superiority over a commercial product, replace independent production-design
validation, or constitute signoff qualification.

## Symbolic Boolean conformance

Install a current checkout with `python -m pip install -e ".[formal]"`, then run:

```console
python benchmarks/symbolic.py --repeat 3 --json-output symbolic-results.json
```

The suite checks six control/mutant formulas at 12, 64, and 128 inputs plus a guarded multiplexer
and a contradictory guard. Independent Boolean identities determine expected equivalence and
exact mismatch assignments. Repeated results must be deterministic; host-dependent timing samples
are excluded from the evidence digest. A correct contradictory-guard outcome is `vacuous`, never a
proof. All eight expected outcomes are regression gates, not a production-accuracy percentage.

The legacy truth-table backend is measured at its 12-variable cap and explicitly declines larger
inputs. A declined comparison's runtime is not meaningful speedup evidence. Measurements compare
OpenCollate backends on synthetic formulas, not licensed commercial products or industrial SoCs.
Actual parser-to-engine 64-input RTL/Liberty control/mutant coverage is separately exercised by
`tests/test_symbolic_integration.py`. See [the exact semantics and limits](../docs/symbolic-verification.md).

## Incremental full-check and upstream cell-interface evidence

```console
python benchmarks/incremental.py --cells 128 --table-rows 256 --repeat 3 --json-output incremental.json
python benchmarks/incremental.py --cells 512 --table-rows 512 --repeat 3 --json-output incremental-large.json
python benchmarks/upstream_cells.py --json-output upstream-cells.json
```

The first runner measures full uncached, cold-cache and warm-cache paths, including
hashing, decoding, reconciliation and rules; process startup is excluded. Each report
must be identical. One source-file direction mutation must trigger OC4001 and match
an uncached check while the other view is reused. Input dimensions and host timings
are published; no speed ratio is enforced as a portable unit-test expectation.
The synthetic timing-table contents are deliberately ignored by this structural tool.

The second runner validates two unmodified upstream RTL/LEF cell-interface controls,
then six mutations applied to temporary source files before parsing. The independent
pin/direction/rail inventory oracle, source commit and file hashes are public. The
corpus is small and does not establish full-chip, functional or foundry qualification.
Neither benchmark runs or claims a win against a proprietary product. See
[methodology and boundaries](../docs/incremental-and-guarded-checks.md).

## Source-bound sequential verification

`python benchmarks/sequential.py --repeat 3 --json-output sequential-results.json` exercises
12 source-file cases: proven pipelines, independently replayed bugs, proof-depth boundaries,
missing guard coverage and unsupported clocks. Scale tiers extend to 256-bit, 16-stage pipelines,
not arbitrary production SoCs. Stable outcome digests are separate from measured elapsed samples.
See [methodology and trust boundaries](../docs/source-bound-sequential.md).

## Independent Boolean certificate corpus

Run `python -m benchmarks.certificates --repeat 5 --json-output certificates.json
--export-dir certificate-evidence` (on one command line). Fifteen expected outcome cases cover
12/64/128/512-input equivalent and faulty formulas, guards, parity, unsupported syntax and work
exhaustion; eight recomputed-checksum forgeries must be rejected. Output retains raw generation
and solver-free verification timings, input and implementation digests, certificate sizes and
producer version. The export contains seven DIMACS/RUP pairs for independent DRAT-trim checking.
See [exact semantics and trust boundaries](../docs/independent-boolean-certificates.md). Neither
local timing nor this finite synthetic corpus establishes commercial superiority or production scale.
