# Source-bound synchronous RTL verification

This development capability reads actual RTL source bytes with pyslang, constructs a typed
transition system, and checks explicitly declared properties using Z3. It does not accept
user-written transition equations as a substitute for inspecting RTL. Install the current checkout
with `python -m pip install -e ".[formal]"`; the older v0.3.0 tag does not contain this command.

```console
opencollate sequential check examples/sequential/request.json --output receipt.json
opencollate sequential replay examples/sequential/request.json receipt.json --output replayed.json
opencollate schema sequential-request
opencollate schema sequential-receipt
```

For an external deadline covering the native frontend as well as solving:

```console
opencollate guard --wall-seconds 60 --status-output watchdog.json -- \
  sequential check examples/sequential/request.json --output receipt.json
```

Check the command exit and watchdog record, not the mere existence of an older output file.

## Precisely what is checked

Samples are taken **immediately before consecutive positive clock edges**. State at sample `t+1`
is the result of all nonblocking assignments at edge `t`; every right-hand side reads the old
state. Procedural assignment priority and implicit hold branches are preserved.

A connection property states `sink(t) == source(t-latency)`. A constant property states
`sink(t) == equals`. All values are finite-width bit patterns; integer comparisons in property
JSON do not reinterpret signed values. Each optional `when` row requires a named signal to equal
a constant at sample `t-lag`. All guards must hold together. An enabled two-stage pipeline needs
enable guards at **both** lag 1 and lag 2. Missing the necessary guard is a genuine different
property, not a tool option that is inferred automatically.

`reset` fixes a scalar primary input active for the stated number of initial edges, then inactive
forever. Resets are synchronous. `assumptions` fixes named primary data inputs for every sample;
it cannot constrain internal state or overlap the reset protocol. All other inputs are arbitrary
at every edge. Initial register contents are arbitrary two-valued patterns, not invented zeros
and not simulation X or language-default initialization. The proof is explicitly for this hardware
startup model. It is not four-state SystemVerilog simulation equivalence.

The first checked sample defaults to `reset.cycles + max(latency, guard lags)`. A property can set
`start_cycle` explicitly to check an earlier reset response or a later steady-state requirement.
No claim is made about samples excluded by this boundary. Properties, reset protocol, all fixed
input assumptions, source names/content hashes and translation digests are bound into the receipt.

## Bounded checking is not proof

| Per-property status | Meaning | Exit |
| --- | --- | ---: |
| `proven` | Bounded base checks and a k-inductive step passed; guard activation was reachable | 0 |
| `counterexample` | Earliest violating sample has a canonical complete trace replayed by the integer interpreter | 1 |
| `bounded` | No counterexample through the stated depth, but no inductive proof | 2 |
| `uncovered` | No reachable guard activation found through the stated depth | 2 |
| `inconclusive` | Resource exhaustion or backend failure | 2 |
| `invalid-or-unsupported` | Request, HDL, source access or elaboration is invalid/unsupported | 2 |

For multiple properties, exit 2 takes precedence over exit 1; individual outcomes remain visible.
An uncovered guard is **not** proven unreachable forever. A SAT induction-step model may be
unreachable from reset; it does not become a design counterexample. A six-cycle clean check of the
included late-failure counter remains `bounded`, while deeper checking finds its cycle-10 failure.

The base solver checks every requested sample from arbitrary initial state with the explicit reset
prefix. The induction solver starts from arbitrary state under stationary post-reset assumptions.
For history length H, it checks whether H+k transitions and k consecutive property hypotheses can
lead to a property failure. An UNSAT step is accepted only with enough verified base samples to
cover the reset/history prefix and those k steps. These are ordinary BMC and k-induction methods,
not a new solver algorithm.

Counterexamples are refined to the lexicographically smallest initial-state and input sequence
(unsigned bit patterns, zero first), then replayed through a separately implemented integer IR
interpreter. Every intermediate combinational value, state update, reset/assumption, and earliest
failure must match. The trace includes source locations and can be independently inspected.

## Supported HDL and deliberate rejection

The selected top may contain a **single-clock module hierarchy**. Parameter defaults/overrides, scalar or simple
packed 1..256-bit values, signed/unsigned arithmetic, comparisons, shifts, reductions, ternaries,
concatenations, constant bit/part selects, continuous assignments and positive-edge
`always`/`always_ff` blocks with nonblocking whole-signal assignments and ordinary `if/else` are
supported. Packed integral enums, ordinary case, and definitely-assigned `always_comb` blocks
are also supported; see [controller semantics](controller-verification.md). Declared packed vectors must use descending `[width-1:0]` indices. Synchronous resets,
enables, pipelines, counters, wrapping arithmetic and assignment priority are modeled.

Unsupported constructs cause an explicit refusal of the analysis, not a black-box proof:
multiple or gated clocks, asynchronous resets,
X/Z literals, inout/ref ports, special net resolution/strengths, declaration initializers,
initial/final blocks, procedural blocking clocked/latch blocks, procedural loops, wildcard or
unique/priority cases, dynamic selects,
partial clocked left-hand-side assignments, function calls, division/modulus, real/array/struct data,
arbitrary delays, and unrestricted preprocessing. Without an explicit `preprocess` object,
all backticks are conservatively rejected, including those in comments. The opt-in
[manifest loader](manifest-preprocessing.md) supports declared literal headers and native macros. Undriven observed signals, multiple drivers
and combinational cycles are rejected. This is not a full SystemVerilog or SVA frontend.

## Replay and trust

Receipts bind exact source bytes read once and used for elaboration, the normalized request, the
lowered IR, frontend version and implementation source digest. `sequential replay` reads current
source files, reconstructs the transition system and re-runs every property. Changing only RTL,
changing assumptions, or changing an outcome and recomputing a receipt checksum is not enough to
make replay accept the result. Input/output path aliases and hard links are protected.

Hashes are **not signatures** or supplier authentication. UNSAT/proven results trust pyslang,
the lowering, Z3 and the induction implementation. Re-solving with the same implementation is
not independent proof-certificate validation. The separate development commands `sequential certify`
and `sequential verify-certificate` now offer source-bound RUP certificates with a solver-free
receiver; ordinary receipts do not contain those proofs. See [proof certificates](proof-certificates.md)
for the independent encoding, proof kernel, tests and remaining frontend/kernel trust boundary.
Counterexample replay independently evaluates the IR, not every SystemVerilog construct. Native
slang constant evaluation and optional Icarus simulation are separate test oracles.

The native frontend and solver are trusted executable dependencies. This feature is not a sandbox,
CDC/RDC check, liveness proof, multi-clock verification, power-intent validation, safety
certification, or signoff. Existing `check` connectivity rules are unchanged; use this additional
command deliberately for the properties and models it supports.

## Bounds and reproducible evidence

Inputs are limited to 32 source files, 1 MiB/file, 4 MiB total; request/receipt JSON to 8 MiB and
64 nesting levels. Paths remain inside the request directory. Models are bounded to 1024 instances, 4096 scopes, 32 scope-depth steps,
16384 elaborated members, 16384 state bits, 32768 IR nodes and bounded lowering work/depth. Up to 32 properties,
128 BMC steps, induction depth 16 and history length 16 are supported. The shared solving deadline
defaults to 10 seconds, with 8192 queries and a per-query Z3 resource limit of 1000000. Runtime
limits are cooperative; the external `guard` command is needed for a process-level wall deadline.

```console
python benchmarks/sequential.py --repeat 3 --json-output sequential-results.json
pytest -q tests/test_sequential.py tests/test_sequential_oracles.py
```

The 12-case source-file benchmark includes correct pipelines through 256 bits x 16 stages,
inversion, wrong latency, missing guards, bounded-only results, uncovered guards, late bugs and
unsupported asynchronous resets. Timings include frontend/IR/solver work, but not interpreter
startup. This regular pipeline corpus does not demonstrate arbitrary 4096-bit state-machine
performance, production-SoC scalability, or superiority over a commercial formal tool.

The native arithmetic oracle covers 31 expression forms, both signedness modes, and all 256
pairs of 4-bit input values: 15872 assignments. Optional Icarus tests replay generated bug traces
against the original RTL in a separate simulator. They are skipped when Icarus is unavailable;
the dedicated GitHub workflow installs it and requires these tests to execute.

Primary method references: N. Bjørner et al., Programming Z3, section 5.4
(https://theory.stanford.edu/~nikolaj/programmingz3.html); slang AST/user documentation
(https://sv-lang.com/user-manual.html). Public finite tests are validation evidence, not proof
that the implementation itself is free of bugs.

For module/generate hierarchy, static output slices, clock aliases, and exact property-cone
reduction, read [hierarchical sequential verification](hierarchical-sequential.md).
