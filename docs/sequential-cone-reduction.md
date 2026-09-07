# Property-local sequential solving

Current development builds reduce each source-bound sequential property to its
transitive cone of influence before BMC and induction. This is an exact dependency
projection within the existing supported two-valued, flat, synchronous subset,
not a new formal engine or support for additional HDL syntax.

## User-facing behavior

The existing `opencollate sequential check` and `sequential replay` commands use
projection automatically. Request and receipt schemas, status distinctions,
reset semantics and source-validation boundaries do not change. The complete RTL
is still loaded and validated **before** any projection. An unsupported construct
in an unrelated block still rejects the analysis.

A property seeds its sink, source, every guard signal, fixed primary-input
assumptions and the reset input. The closure follows both combinational drivers
and register next-state functions until no dependency remains. It is not truncated
at the requested latency or BMC depth. Feedback, state-dependent guards, reset
conditions and enables therefore remain in the solver whenever they can influence
the property. Nodes are compacted in topological order and the resulting circuit
is finalized again to detect unresolved dependencies or cycles.

The receipt binds the **original full** source bytes and full IR, not only the
cone. Editing unrelated source still invalidates an existing receipt. The
implementation digest includes the projection module, so receipts from an older
implementation require fresh checking rather than silently changing meaning.

## Why the projection preserves this model's property

Every retained output equation and next-state function references only retained
signals. Full-model executions therefore project to cone executions. Conversely,
choose arbitrary initial values and input sequences for omitted signals and
execute their total, deterministic equations: every cone execution extends to a
full execution with the same property observations. All allowed environment
constraints are fixed primary-input values or the explicit reset protocol; their
inputs are retained. No internal-state assumptions, fairness conditions, unknown
black boxes or partial transition relations are introduced here.

The same correspondence applies to the stationary induction model. A retained
state's entire update is preserved even when a BMC trace would not yet reach a
long dependency chain. This reasoning relies on the existing frontend's rejection
of unsupported/ambiguous HDL; it is not a proof that the implementation is bug-free.

## Complete counterexamples, not partial witnesses

The cone solver still minimizes initial state and inputs in the original
unsigned, lexicographic order restricted to retained variables. Omitted initial
state and primary inputs can independently be zero, their lexicographic minimum.
The integer interpreter then reconstructs every full-circuit sample. Omitted
register values at **later** samples are computed from their actual updates, not
filled with zeros. Every retained value is checked against this execution.

The complete trace must pass the existing full-circuit replay, including input
assumptions, reset, all combinational equations, every state transition and the
earliest property failure, before a counterexample is published. Projection,
reconstruction, replay or deadline failures stay inconclusive.

## Reproducible validation

```console
python -m pip install -e ".[dev]"
pytest -q tests/test_sequential_cone.py
python benchmarks/sequential_cone.py --repeat 3 --json-output cone-results.json
```

The original `sequential_smt.verify_property` remains the unreduced regression
oracle; `sequential_cone.verify_cone_property` wraps it with projection and full
trace reconstruction. Differential tests compare complete outcomes and complete
canonical traces over correct and faulty pipelines, missing guards, unreachable
guards, initial-state failures, bounded-only checks, state-dependent guards and
feedback, at widths 1, 4, 16, 64 and 256. These two paths share the frontend and SMT
engine; agreement is not an independent proof of HDL semantics. Existing native
arithmetic and Icarus simulator oracles remain part of validation.

Pure integer tests exhaustively compare 512 small-state/input combinations for
one transition, check projection idempotence, and reject corrupted traces.
The benchmark retains every timing sample, source hashes, normalized requests,
state/node counts and exact-result checks. It alternates execution order and
includes a dense no-reduction control. Timings isolate the solver stage; frontend,
receipt hashing and interpreter startup are not part of reported speedups.

Benefits depend on locality: a small property amid unrelated logic can shrink
substantially; a property depending on nearly the whole design may not improve
and incurs projection overhead. Full parsing, validation, source binding and
full-counterexample reconstruction still scale with the full design. Synthetic
results do not establish production-SoC scalability, signoff qualification or
superiority over proprietary tools. Multi-clock, hierarchy and other unsupported
constructs remain unsupported.
