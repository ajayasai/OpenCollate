# Bounded symbolic Boolean verification

This capability is available on the development branch after v0.3.0. Install the optional backend
from a current checkout, not the older v0.3.0 tag:

```console
python -m pip install -e ".[formal]"
```

## RTL/Liberty comparison

The legacy truth-table backend remains the default. To select Z3 for the existing supported
combinational RTL/Liberty function checks, add these keys to the project's existing policy table:

```toml
[policy]
boolean_backend = "z3"
max_symbolic_inputs = 512
symbolic_timeout_ms = 5000
symbolic_resource_limit = 1000000
```

Then run the normal check or baseline review. Input aliases, source evidence, mismatch rule OC4301,
waivers, and existing reporters still participate. Selecting Z3 does not expand the RTL parser's
supported function subset or silently interpret procedural/sequential logic as combinational.
An unavailable selected backend, solver failure, or exhausted symbolic budget becomes fatal OC4302
and exit status 2. Incomplete symbolic analysis cannot become a successful CI check.

The implementation lowers OpenCollate's validated Boolean IR into a fresh Z3 context per call. It
uses a satisfiability check of the XOR miter, rather than enumerating all input assignments. A SAT
result is refined to the lexicographically first complete assignment (False before True) and
replayed through a separate iterative Python IR evaluator. Incorrect witnesses are inconclusive.

## Guarded obligations and replay

A standalone obligation states equality under an explicit assumption:

```json
{
  "schema_version": 1,
  "semantics": "two-valued-combinational",
  "obligations": [
    {
      "id": "selected-route",
      "left": "(S & A) | (!S & B)",
      "right": "A",
      "assume": "S"
    }
  ]
}
```

No native solver script, Tcl, Python, or subprocess command is accepted. Unknown JSON fields,
duplicate keys, duplicate obligation identifiers, incompatible semantics, and oversized input are
rejected. The omitted `assume` default is Boolean true (`"1"`).

```console
opencollate formal check examples/formal/obligations.json --output receipt.json
opencollate formal replay examples/formal/obligations.json receipt.json --output replay.json
opencollate schema formal-request
opencollate schema formal-receipt
```

Both commands accept `--max-variables`, `--timeout-ms`, and `--resource-limit`. The sample has two
valid equivalent obligations and returns 0. Replacing the route assumption with `"1"` produces a
counterexample; replacing it with `"S & !S"` produces a vacuous result, not a proof.

| Result | Interpretation | Exit status |
| --- | --- | ---: |
| equivalent | Equal for all two-valued assignments satisfying a satisfiable guard | 0 |
| different | A deterministic mismatch assignment was independently replayed | 1 |
| vacuous | The guard is contradictory, so no useful equivalence is established | 2 |
| inconclusive | A limit, unsupported expression, missing backend, or backend error prevents a conclusion | 2 |

Across multiple obligations, exit 2 takes precedence over exit 1. The JSON retains every result,
including demonstrated mismatches even when another obligation is incomplete.

Receipts bind normalized requests, identifiers, results, semantics, and content digests. Replay
checks that binding, then recomputes every obligation and compares stable semantic outputs. It does
not accept a foreign tool's `pass` field as evidence. Backend versions and counters are recorded but
can differ on a new run. Changes to formulas or assumptions invalidate request binding.

## Exact trust boundary

This is **two-valued combinational reasoning**, not temporal verification, sequential equivalence,
protocol verification, CDC/RDC, X/Z analysis, timing verification, or tapeout signoff. Standalone
obligations bind the submitted formulas, not the original RTL files or the correctness of a user's
translation into those formulas. They are not automatically extracted sequential proof obligations.

UNSAT results rely on Z3 and OpenCollate's IR translation. No independent UNSAT certificate checker
is implemented. Receipt replay repeats solving; it is not independent proof-certificate validation.
SHA-256 digests detect accidental corruption or mismatched content, but are not signatures or
sender authentication. Someone who changes content and recomputes its digest has not been
cryptographically authenticated; replay still recomputes the declared property.

The public Python `SymbolicLimits` defaults cap variables at 512, IR nodes at 32768, queries at
1024, per-query solver resources at 1000000, and total elapsed solving/witness work at 5000 ms.
The CLI sets its query cap to `max_variables + 2`. The solver timeout is cooperative. Input and
query caps are not OS-enforced memory limits or a sandbox for the native solver. Applications with
strict process-level budgets should run the command in their own restricted worker/container.

## Public evidence

`python benchmarks/symbolic.py --json-output symbolic-results.json` checks independent AND/De Morgan
oracles at 12, 64, and 128 inputs, mutant counterexamples, satisfiable mode assumptions, and vacuity.
The legacy backend declines cases above its configured 12-variable cap; declining is not a speed
comparison. Results measure synthetic formulas, not production SoCs or proprietary competitors.
`tests/test_symbolic_integration.py` separately runs a 64-input RTL/Liberty file pair through the
actual parser and comparison engine. Property tests compare random small formulas and guards with
exhaustive truth-table enumeration. These finite corpora are not evidence of universal superiority.
