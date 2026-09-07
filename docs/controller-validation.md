# Hierarchical controller validation

This development upgrade integrates the earlier unmerged hierarchy/cone work
with the independent-certificate baseline at
`58e724148f5d29a98f641abeca51bcd4b2fd5ae7`, then adds ordinary procedural
controller semantics. It does not establish superiority over a proprietary tool.

## Local run, 7 September 2026

On Linux x86-64 / Python 3.13.5, the full suite and optimized-Python suite each
passed 1,363 tests; two Chromium tests were delegated to the existing browser CI
job. Icarus was installed and all simulator oracles executed. Combined statement
and branch coverage was 87.09%, above the unchanged 85% gate; standalone branch
coverage was 81.92%.

The new controller corpus has 12 explicit expectations: three positive source
checks/certificates, three source mutants, four unsupported constructs, an
uncovered guard and a bounded-only case. Every case was repeated three times.
Positive cases require both full/reduced Z3 agreement and solver-free full-model
RUP verification. Mutants require matching full counterexample traces and
specific certificate rejection. Arbitrary exceptions are not accepted as negative
case success. Sources, requests, complete raw timings, certificate sizes and
outcome digests are retained in
[the controller results](../benchmarks/results/controllers-2026-09-07.json).

The independent Icarus tests compare 2,048 signed/unsigned combinational input
triples with controller values and replay three controller counterexamples. The
three hierarchy simulator tests also run; their parser was corrected to select
explicitly prefixed trace rows rather than treating Icarus's final `$finish`
notice as a data sample. Exact trace-length and value comparisons remain.

The existing 34-pair semantic mutation, 8-case symbolic, 12-case sequential,
12-case certificate, 8-case upstream-cell and 4-case conformance corpora passed.
A separate wheel installation containing neither Z3 nor PySAT successfully
checked both hierarchical and enumerated-controller certificates.

## Performance measurement

The synthetic 131-instance hierarchy has 4,112 state bits and 786 IR nodes; the
requested property uses 16 state bits and 12 IR nodes. Three-sample isolated
medians were 0.8989 seconds full and 0.2345 seconds reduced, a 3.83x ratio. Full
and reduced clean outcomes and mutant full traces matched. The included
zero-distractor control measured a 1.04x ratio, demonstrating that reduction is
not a universal acceleration. Raw samples:

- [128-distractor hierarchy](../benchmarks/results/hierarchy-cone-2026-09-07.json)
- [Zero-distractor control](../benchmarks/results/hierarchy-control-2026-09-07.json)

These measure the same upgraded OpenCollate with reduction enabled/disabled, not
commercial tools. They include source/frontend/IR/solver work, exclude interpreter
startup, and do not establish arbitrary state-machine or production-SoC capacity.
Certificate generation and reception deliberately retain full-model obligations;
the cone timing does not claim faster certificate checking.

## Reproduction and limits

Run `pytest -q`, `python -O -m pytest -q`, `python -m benchmarks.controllers
--repeat 3`, and `python -m benchmarks.hierarchy --distractors 128 --repeat 3`
from the updated checkout. Install Icarus for independent simulator tests.
The dedicated hierarchy/controller CI job installs it and checks certificates
again in a separate solver-free receiver environment.

See [controller semantics](controller-verification.md),
[hierarchy and reduction](hierarchical-sequential.md), and
[certificate trust boundaries](proof-certificates.md). Frontend/lowering/encoding
correctness is tested, not mechanically proved; this is not full SystemVerilog,
SVA, multi-clock verification, liveness, four-state simulation, or signoff.
