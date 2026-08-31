# Public benchmark and conformance suite

This suite measures real, redistributable OpenCollate workflows without any
proprietary tools or datasets:

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

JSON is emitted with sorted keys, no timestamp, normalized repository-relative
paths, deterministic result SHA-256 digests, and explicit conformance oracles.
Every emitted report validates against the bundled Draft 2020-12
[`benchmark-results.schema.json`](benchmark-results.schema.json).
Elapsed samples necessarily depend on the host, Python build, filesystem, and
system load. The default 30-second ceilings are deliberately generous guards
against pathological regressions, not performance promises.

These results compare OpenCollate releases on equivalent public hardware and
inputs. They do not establish feature or performance superiority over a
commercial product, and they are not a substitute for signoff qualification.
