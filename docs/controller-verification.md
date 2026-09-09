# Hierarchical controller verification

This development upgrade integrates the earlier hierarchy/cone patch with the newer independent
certificate checker, and adds actual procedural controller lowering. It is not a general
SystemVerilog interpreter and does not claim superiority over a proprietary tool.

## Run the examples

```console
python -m pip install -e ".[formal,certificates]"
opencollate sequential check examples/controller/request.json --output result.json
opencollate sequential certify examples/controller/request.json --output certificate.json
opencollate sequential verify-certificate examples/controller/request.json certificate.json
opencollate sequential check examples/controller/fsm-request.json --output fsm.json
```

`controller.sv` instantiates a parameterized byte-wide controller. A plain case decodes load,
count and XOR commands. Its property requires a load at one edge to appear at the output at
the next sample. `fsm.sv` uses a packed integral enum and combinational next-state logic; its
properties check idle/working output behavior and require reachable guard activation.

## Procedural semantics and conservative rejection

Supported `always_comb` bodies contain ordinary sequential blocks, whole-signal blocking
assignments, ordinary if/else and plain case. The frontend reads typed, elaborated pyslang AST
nodes; it does not extract RTL behavior with regular expressions or execute HDL as Python.

Within a blocking block, each RHS reads the most recent definitely assigned value. Assignment
reinterprets that value at the target's declared signedness before later reads. Branches receive
separate environments and merge only values defined on both paths. Every variable written by
a block must be definitely assigned by its end. A read of a variable written by that same block
before definite assignment is rejected even when a user believes it is harmless. This prevents
order-sensitive blocks or an implicit latch from becoming a combinational proof.

These checks are deliberately syntactic and conservative. An exhaustive case without a default
or preceding default assignment can be rejected; the frontend does not prove that missing branches
are unreachable. A variable that is unconditionally overwritten before it is read can safely have
an earlier incomplete assignment. Stateful blocks continue to read old values for every nonblocking
RHS; statement priority and implicit holds remain unchanged.

Plain case evaluates its selector and labels in the pre-case procedural environment. Label groups
are ORed and the first matching group wins, including overlapping groups. The default applies only
if none match regardless of its textual position. All input bit patterns are two-valued. `casez`,
`casex`, case-inside/pattern cases and `unique`/`unique0`/`priority` qualifiers are rejected rather
than silently dropping wildcard semantics or assertions.

Packed integral enums and scalar/packed-vector type aliases use their declared finite-width
storage, including signedness. Initial enum state may contain any two-valued storage bit pattern,
not just a named enumerator, under the existing hardware startup model. No extra initial-state
assumption is silently introduced. Ordinary reset and default branches handle such states.

Only module/generate-level signal declarations are supported. Procedural locals/named scopes,
procedural loops, functions/tasks, partial procedural LHS assignments, compound assignments,
nonblocking combinational assignments, blocking clocked assignments, latches, general event lists,
unrestricted preprocessing (see [manifest mode](manifest-preprocessing.md)), interfaces/modports, multiple/gated clocks, asynchronous reset and four-state
semantics remain outside this feature. Each case is bounded to 256 groups and 1024 labels;
existing expression depth, lowering work, source size and model limits also apply.

## Verification paths and source binding

Both the producer and receiver validate the complete instantiated source, including unsupported
logic unrelated to a requested property. The Z3 path then computes an exact dependency-closed
property cone and can expand counterexamples back to the complete state trace. `--no-cone`
selects the unreduced Z3 path for differential checks.

The independently checked certificate path deliberately continues to construct full-model
base/induction CNF and full-state guard witnesses. It does not accept Z3 results or assume that
an imported cone is correct. Verification neither imports nor invokes a SAT/SMT solver. It
still trusts the frontend, lowering, Boolean encoding and Python RUP kernel; none is mechanically
verified. The shared lowering means independent certificate checking alone is not an independent
SystemVerilog semantic oracle. Separate Icarus tests compare original HDL execution.

IR changes invalidate old receipts and certificates: regenerate them from the original source.
No results are migrated by relabeling old proof evidence. Hashes are source/content bindings,
not supplier authentication or digital signatures. Existing watchdog and output-alias protections
remain active. Use a fresh watchdog status path for every run.

## Reproducible evidence

```console
pytest -q tests/test_sequential_procedural.py tests/test_hierarchy_oracles.py
python -m benchmarks.controllers --repeat 3 --json-output controllers.json
python -m benchmarks.hierarchy --distractors 128 --repeat 3 --json-output hierarchy.json
```

The 12-case source corpus covers three valid controllers and nine specific bug/incompleteness
outcomes. Positive cases require a verified certificate; negative cases require the expected
reason, not an arbitrary exception. The 16-controller case records 128 total register bits and
an 8-bit cone for the selected load property. The certificate receiver checks the full model.

Independent Icarus tests exercise all 1024 input combinations of a four-bit blocking/priority
example in both signedness modes, compare all intermediate values, and replay three complete
hierarchical controller counterexamples. Three previously unrun hierarchy Icarus tests also run;
the harness now distinguishes tagged signal frames from simulator termination messages.
Tests without Icarus explicitly skip these oracles; the dedicated workflow installs it.

The source corpus is synthetic and structured. It does not establish industrial capacity,
complete language coverage, functional safety, signoff, or commercial-tool superiority. Measured
results are retained separately from the implementation and include dependency versions.
Primary frontend reference: https://sv-lang.com/user-manual.html and its CaseStatement AST API.
