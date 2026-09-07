# Hierarchical sequential verification and property-cone reduction

This development upgrade extends `sequential check`; it does not change the static
connectivity meaning of the regular `check` command. Use the upgraded checkout with
`python -m pip install -e ".[formal]"`. The v0.3.0 tag does not contain this capability.

## Actual elaborated hierarchy

The frontend walks each instantiated **body**, not a shared cached module definition.
Parameterized instances, named/positional/implicit port connections, instance arrays,
and elaborated for/if/case generate scopes are supported. Inactive generate branches
are omitted because they are not instantiated hardware; unsupported **active** logic
is still an error, even outside every requested property's cone.

Signals keep unambiguous top-relative elaborated paths, such as `a.q`, `g[2].u.q`, or
`lanes[3].ff.en`. The selected top's own signals retain their old names. Escaped names,
non-ASCII identifiers, dynamic indices, ambiguous partial paths, and path guessing are
not supported. Paths have at most 256 characters; top, clock, reset, and fixed-input
assumptions still name simple top-level signals. Generate indices can be negative.

The frontend uses the typed input/output conversion expressions supplied by slang.
Signed extension, zero extension, truncation, constant slices, and concatenation ordering
therefore follow those elaborated widths. Whole-signal and constant disjoint bit/part
outputs are assembled explicitly. Overlapping writes, incompletely driven output words,
unconnected child inputs, and input/output direction ambiguities are rejected. Unused
unconnected child outputs are allowed; they are not implicitly given arbitrary values.
Clocked procedural assignments still require whole-signal nonblocking targets.

Clock wiring is deliberately strict: scalar clock **wire aliases** may pass through
module ports or direct continuous assignments. Every actual clock event must trace to
the one chosen positive-edge top clock. A gate, inversion, selector, asynchronous event,
second clock, or clock used as data is rejected, rather than being assigned invented
cycle semantics. Module port data expressions do not execute Tcl or arbitrary Python.

The receipt records instance paths/definitions/source locations and all recognized clock
aliases. Source hashes still cover every submitted file, including inactive source text.

## Work a generated-lane example

```console
opencollate sequential check examples/hierarchy/request.json --output receipt.json
opencollate sequential replay examples/hierarchy/request.json receipt.json --output replayed.json
```

`examples/hierarchy/design.sv` has eight parameterized 1-bit lane instances and a separate
32-bit heartbeat counter. The request proves both the full-byte transfer and a property
on one internal lane. The full model has 40 state bits; the respective structural cones
contain eight and one state bits. The required enable guard is explicit; removing it
asks a different property and may produce a counterexample.

## Exact structural cone reduction

After **the complete instantiated design has been validated**, each property's sink,
source, guards, and historical signal references seed a backwards dependency closure.
Combinational equations and state-update equations are followed until a fixed point,
including feedback. All primary inputs, reset protocol, and fixed-input assumptions
remain present. This is signal-level structural reduction, not bit-level slicing,
abstraction of unsupported hardware, invariant guessing, or a new solver algorithm.

For this total deterministic transition IR, every reduced trace extends to the complete
model: omitted state can start arbitrarily and then evolves using its original equations.
The reverse projection preserves every retained equation. Thus dropped state cannot
constrain or alter the property, reset protocol, or guard activation. This reasoning
would not justify dropping state constraints, fairness assumptions, liveness conditions,
or black-box relations; those features are not accepted by this verifier.

To preserve debugging evidence, a reduced counterexample is extended with zero-valued
omitted initial state, then **all subsequent omitted values are simulated**, not zeroed.
The entire trace is independently replayed against the unreduced IR, including the
original earliest-failure test. Any failed extension/replay makes the result inconclusive.
Full and reduced modes are tested for identical outcomes and complete witness traces.

```console
opencollate sequential check examples/hierarchy/request.json --no-cone --output full.json
opencollate sequential replay examples/hierarchy/request.json full.json --no-cone
```

`--no-cone` is the explicit differential-validation/reference path. Replay must use the
saved mode. Receipts distinguish full-model size from per-property `proof_cones` size;
reduction does not change source binding. The algorithm/implementation binding changes
in this upgrade, so old proof receipts must be regenerated, not relabeled as new proofs.

## Bounds, evidence, and trust

There are at most 1,024 module instances, 4,096 visited scopes, 32 scope-depth steps,
16,384 elaborated non-scope members, 16,384 full-model state bits, and 32,768 full-model
IR nodes. The full-model limits apply **before** reduction. Source bytes, properties,
BMC depth, induction depth, and solver deadlines retain their existing bounds. Native
elaboration precedes these post-elaboration checks; `guard` remains necessary for an
external wall deadline. This feature is not a security sandbox.

The `benchmarks/hierarchy.py` corpus elaborates actual generated RTL source and compares
the same checker with reduction on/off. Timing includes source reading, frontend, IR,
cone construction, and solving, but excludes interpreter startup. Every run verifies
identical clean outcomes; a source-level mutation verifies identical full counterexample
traces. These structured examples are not production-SoC or proprietary-tool comparisons.

Tests cover per-instance identity, parameter overrides, signed port conversion, generated
indices, output slices/concatenations, no black-boxing of unsupported descendants, exact
feedback/guard closure, differential full-model solving, and randomized trace projection.
Optional Icarus tests simulate original hierarchical RTL; they are skipped when Icarus
is absent. A separate read-only workflow installs Icarus and runs those tests.

Remaining boundaries include full SystemVerilog/SVA, procedural `case`/loops/`always_comb`,
preprocessing, interfaces/modports, asynchronous reset, multi-clock verification, four-state
semantics, liveness, industrial qualification, and independently checked UNSAT certificates.
Proven results still trust slang, translation, cone selection, Z3, and induction. Open tests
and source hashes do not constitute safety certification or demonstrate universal superiority.

Primary background: [slang elaboration](https://sv-lang.com/overview.html) and
[NuSMV's cone-of-influence capability](https://nusmv.fbk.eu/articles/271/).
