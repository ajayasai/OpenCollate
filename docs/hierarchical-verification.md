# Hierarchical sequential verification and dependency-closed reduction

This development capability builds on source-bound sequential verification. Use a
current checkout with the `formal` extra, not the older v0.3.0 release tag.

## What is implemented

The pyslang frontend elaborates the selected design. OpenCollate visits every
active module and generate scope, assigns collision-free top-relative signal
paths, and lowers all supported drivers into the same finite-width transition
system. Identical instances have independent state. Named/positional port
connections, parameter overrides, constant input expressions, and frontend-
elaborated truncation/sign extension are preserved. Generated names such as
`peripherals[3].c.count` can be property endpoints or guards.

A single primary scalar positive-edge clock can be forwarded directly through
input ports across module boundaries, even with different local names. A clock
cannot become data, an unconstrained input, or a guessed gated-clock domain.
Continuous clock aliases, computed/gated clocks, multiple clocks and asynchronous
reset remain unsupported. Existing two-valued startup/reset/history semantics,
proof statuses, and exit-code precedence are unchanged.

Supported scope additions are ordinary named module instances and active
`if`/`case`/`for` generate scopes already elaborated by pyslang. Procedural `case`
or loops are NOT added. Inactive branches are outside this elaboration profile,
not a promise to verify other parameter values. Instance arrays, interfaces,
complex ports, partial output-port lvalues, arrays/memories, procedural blocking
logic, four-state behavior and preprocessing remain unsupported. Missing inputs,
undriven outputs, multiple drivers, and combinational cycles fail closed.

## Full validation precedes reduction

For each property the checker starts from the sink, source, history guards,
reset input, and all fixed input assumptions. It follows dependencies through
combinational equations AND the next-state equation of every encountered
register, to a fixed point. This is a syntactically closed projection, not
black-box abstraction or an assumption that unrelated source is correct.

Every active source construct is validated and lowered first. An unsupported
asynchronous block or combinational cycle in an unrelated instance still rejects
the analysis. Source files are still read once and hashed in full, and the
receipt binds the full model as well as per-property reduced model digests.
Changing even an omitted instance's source invalidates an old receipt.

For a deterministic, total transition system, a dependency-closed projection
preserves the retained signal trajectories under the same inputs, initial
values, reset protocol and assumptions. Induction and base checks then operate
on that exact projected system. Dependency traversal includes control guards,
feedback and history: a register that first matters ten cycles later is not
removed merely because it has no immediate output effect.

Counterexamples are not shipped as partial traces. Omitted initial registers
and free input values are set to zero, the full original model is simulated,
and every projected value is checked for agreement. The entire lifted trace is
then independently replayed, including the omitted equations and earliest
failure. Failure of either replay yields incomplete analysis, never a fabricated
counterexample. With the current canonical-witness ordering, the reference and
reduced paths should return exactly the same full trace; tests enforce this.

## Commands and receipt compatibility

```console
opencollate sequential check examples/hierarchy/request.json --output receipt.json
opencollate sequential replay examples/hierarchy/request.json receipt.json
opencollate sequential check examples/hierarchy/request.json --no-cone-reduction
opencollate sequential check examples/hierarchy/request-mutant.json --output bug.json
```

Reduction is enabled by default. `--no-cone-reduction` retains a full-model audit
path, not a different property definition. Replay follows the mode recorded in
the receipt unless explicitly overridden. A mismatched mode may have different
model metadata and is not silently accepted as the original receipt.

Sequential requests remain schema version 1; only property signal paths are
broadened. Top/clock/reset names and input assumptions remain primary names, not
hierarchical constraints on internal state. Sequential **receipts are version
2**: they record `limits.cone_reduction` and `model.property_cones`, each with
signal/input/state-bit/node counts and a reduced IR digest. Old version-1
receipts must be regenerated from source; changing a version field does not
upgrade a proof. Other report/contract/formal schemas are unchanged.

For an external wall deadline use the existing guard command with a new status
path. Native frontend/solver limits remain cooperative, and this feature is not
a security sandbox. Traversal is bounded to 1,024 child instances, 16,384 active
members and 32 scope levels. Pyslang also bounds instance depth, generate steps
and instance-array elaboration. The existing 16,384-state-bit and 32,768-IR-node
bounds apply to the FULL model before reduction. Graph validation uses indexed
topological ordering and a one-million-node-visit work budget.

## Reproducible evidence and limits of the claim

```console
pytest -q tests/test_sequential_hierarchy.py tests/test_sequential_cones.py
pytest -q tests/test_sequential_hierarchy_oracles.py
python benchmarks/hierarchy.py --repeat 3 --json-output hierarchy-results.json
```

Tests cover independent instance state, nested clock forwarding, parameter
specialization, signed input/output resizing, generated indices, malformed
ports/names, cycles and unsupported active branches. Independent exhaustive
cycle enumeration checks all two-register initial patterns and four-sample
one-bit input/enable sequences. Optional Icarus tests replay every visible
signal in six original-RTL hierarchical counterexamples, including generated
instances, a width bug and state omitted from the proof cone. The dedicated CI
job installs Icarus; a local skip is not a simulator-test pass.

The benchmark uses actual hierarchical source files, not manually supplied IR.
Its target is an 8-bit two-stage pipeline with 0, 16, 64 or 128 unrelated 32-bit
counters. Full and reduced modes use equal 60-second/10-million-per-query budgets
and alternate measurement order. Published ratios are valid only when both
modes prove the property and exactly match the full mutation trace. Timings
include reading, elaboration, lowering, cone construction and solving; not
interpreter startup. Small models can be slower after reduction.

This is not a new verification algorithm, full SystemVerilog/SVA support,
production-SoC validation, independent UNSAT certificate checking, safety
certification, or a head-to-head benchmark against commercial products. Pyslang,
the lowering, Z3, and the induction implementation remain in the proof trust
boundary. The improvement is greater supported composition and less irrelevant
solver work, backed by publicly reproducible finite tests.

Frontend reference: slang AST/port-connection documentation at
https://sv-lang.com/classslang_1_1ast_1_1_port_connection.html and
https://sv-lang.com/user-manual.html. BMC and k-induction methodology remains as
cited in source-bound-sequential.md.
