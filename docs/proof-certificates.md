# Independently checkable source-bound safety certificates

The development checkout can produce a **RUP proof certificate** for the supported synchronous
RTL safety properties. A receiver re-reads the original RTL, reconstructs the Boolean proof
obligations, checks every proof addition, and replays a reachable guard witness. The receiver
does not install, import, or call Z3 or PySAT. This is stronger evidence than trusting a saved
`proven` string or rerunning the same solver. It is not full SystemVerilog support or signoff.

## Run it

From a current source checkout (these commands are not in the older v0.3.0 release tag):

```console
python -m pip install -e ".[certificates]"
opencollate sequential certify examples/sequential/request.json --output proof.json
opencollate sequential verify-certificate examples/sequential/request.json proof.json --output checked.json
opencollate schema sequential-certificate
opencollate schema certificate-verification
```

The producer extra is `python-sat>=1.9.dev15,<2`; the validated environment uses 1.9.dev15,
a development release. Creation uses its Glucose3 backend with DRUP logging. Receivers need
only the normal OpenCollate dependencies and the same supported frontend version; they do not
need the `certificates` or `formal` extras. The original RTL and request must accompany the
certificate, retaining their relative directory structure. Source bytes are never embedded in
this certificate, but design names, paths, witness values and proof evidence remain sensitive.

Both commands return **0 only on complete success**. A missing property, uncovered guard,
invalid proof, resource exhaustion, unsupported source, bounded-only result or counterexample
prevents certificate issuance/acceptance and returns **2**. These are certificate-workflow
statuses, not a replacement for `sequential check`, which still distinguishes counterexamples
with exit 1 and emits full traces. An existing output file is left untouched after failure.
Always inspect the command exit status; an older file's existence does not indicate success.
Successful publication is atomic. Outputs cannot overwrite their source/request/certificate
inputs, including resolved aliases and hard links.

For a process-level deadline covering native parsing, proof generation and Python checking:

```console
opencollate guard --wall-seconds 60 --status-output watchdog.json -- \
  sequential certify examples/sequential/request.json --output proof.json
```

Inspect both the command and watchdog outcome. This is not an OS security sandbox.

## What the certificate establishes

The request and exact HDL subset are the same as [source-bound sequential verification](source-bound-sequential.md).
Samples are immediately before positive edges, updates preserve nonblocking old-state semantics,
initial state is arbitrary and two-valued, and reset/fixed primary-input assumptions are explicit.
The property starts at its declared or normalized `start_cycle`; earlier samples are outside the
claim. A connection compares `sink(t)` with `source(t-latency)`, subject to all declared guards.

For every requested property, a certificate contains three pieces of evidence:

1. **Reachable activation:** a concrete initial-state/input sequence reaching an active guard in
   the checked interval. The integer IR interpreter checks signal inventory, widths, reset,
   assumptions, combinational equations, every transition and final guard activation.
2. **Base refutation:** an UNSAT proof of the disjunction of all property failures from
   `start_cycle` through the requested bounded depth, with the real reset prefix.
3. **Inductive-step refutation:** an UNSAT proof that `k` consecutive property hypotheses can
   lead to failure, with arbitrary initial state and stationary post-reset inputs.

Let `H` be the maximum source latency/guard lag, `R` the reset-prefix length and
`P = max(start_cycle, R + H)`. The receiver permits only `1 <= k <= induction` and
`P + k <= depth`. The step uses samples `H` through `H+k-1` as hypotheses and checks failure at
`H+k`. These coverage requirements connect the verified reset/base prefix to induction;
a shallow clean trace alone cannot produce an unbounded certificate. All requested properties
must be present exactly once in normalized order. No certificate is emitted for an empty or
partial set of accepted properties.

The frontend accepts only deterministic, total transition equations for this supported subset.
The base encoding includes the whole requested horizon; each prefix can be extended under the
fixed-primary-input protocol. There are no hidden internal-state assumptions that can remove
an earlier failure by making future steps impossible.

## Separate encoding and small proof kernel

`sequential_cnf.py` is a separate typed-IR bit blaster, not Z3's bit-blast tactic. It represents
finite-width arithmetic with deterministic AND/XOR Tseitin gates, handles signedness and
oversized shift counts, and preserves the existing IR's widths and comparisons. Initial
registers and unconstrained inputs are free bits. Subsequent registers and combinational wires
are exact aliases of their defining expressions. This substitutes deterministic equations;
it does not assume uninitialized registers are zero or assume reset/enable values that the
request did not specify. It substantially reduces redundant equality clauses in pipelines.

`proof_kernel.py` accepts only bounded arrays of integer clauses. For each added clause `C`,
it assumes the negation of every literal in `C` and requires unit propagation to derive a
contradiction from the original CNF plus previously checked clauses. Thus each addition is a
logical consequence; an explicit final empty clause establishes inconsistency of the original
obligation. Duplicate literals and tautologies are normalized soundly. Watch positions may be
reused, but assignments are reset between additions. Malformed literals, unproved additions,
missing final contradictions, and trailing steps after a contradiction are rejected.

This is a deliberately small **RUP-only** format, not a general DRAT/RAT checker. During
creation, DRUP deletion hints are dropped: retaining already proved clauses preserves soundness.
When a producer omits the final empty step, the producer wrapper adds it but still requires it
to pass the same RUP check. A producer's `UNSAT` return value is never sufficient. Even a
producer's SAT witness is checked against the regenerated CNF before it is used.

The receiver regenerates the CNF rather than accepting a supplied formula. Exact source bytes,
normalized assumptions/properties, frontend identity and IR are content-bound; each obligation
also has a digest and dimension metadata. **Hashes are not proof steps or authentication.**
Recomputing all hashes after changing RTL cannot make an old proof refute the new obligation.
The schema checks structure only; mathematical acceptance requires `verify-certificate`.

## Trust boundary and limitations

The trusted computing base still includes pyslang, HDL-to-IR lowering, the new IR-to-CNF
translation, obligation construction, the integer witness interpreter, the Python RUP kernel
and the runtime. The kernel has adversarial/differential tests but is **not mechanically
verified**. A common error in the frontend or specifications is not eliminated by using an
independent SAT proof checker. The new encoding is tested against the separately implemented
integer IR interpreter; existing tests additionally compare HDL arithmetic with native slang
and replay original-RTL counterexamples in Icarus when installed.

An ordinary Z3 `sequential check` receipt remains solver-trusting; `sequential replay` still
re-solves. It is not automatically upgraded into a RUP certificate. The separate new commands
are required. This implementation does not add hierarchy, general procedural HDL, four-state
semantics, multiple/asynchronous clocks, CDC/RDC checks, liveness, native SVA, arbitrary
production-scale formal capacity, supplier signatures, safety qualification, or tapeout signoff.
It does not establish that commercial tools lack independently checkable evidence.

## Resource limits

The receiver owns the limits; a certificate cannot request higher limits. Defaults are 100,000
CNF variables, 200,000 input/retained learned clauses, 2,000,000 total literals per checked proof,
100,000 proof additions, a shared work budget of 50,000,000 instrumented operations, and a
30-second cooperative deadline. CLI `--timeout-ms` is limited to 120,000 and `--proof-work` to
100,000,000. Ordinary request limits (32 files, 1 MiB/file, 4 MiB total, 32 properties, 128 BMC
steps, 16 induction steps/history, 256 bits per signal) also apply. JSON input/output is limited
to 8 MiB; nesting and duplicate keys are checked by the existing bounded reader.

Work counts are implementation units, not CPU instructions. Native parsing, solver setup and
proof extraction can allocate memory or spend time before Python regains control. The producer
uses a limited SAT call with timer interruption, but a hard end-to-end wall/memory limit still
requires the external guard. The implementation is not a memory-isolation mechanism.

## Reproducible validation

```console
python -m pip install -e ".[dev]"
pytest -q tests/test_proof_kernel.py tests/test_sequential_cnf.py tests/test_sequential_certificates.py
python -m benchmarks.certificates --repeat 3 --json-output certificate-results.json
```

The tests include independent gate truth tables, **14,848 four-bit operator/input combinations**
(29 operators, signed/unsigned modes, all 256 input pairs), 1/4/8/32/256-bit casting and selection
edge cases, 200 seeded small CNFs checked against exhaustive truth tables, and 2,500 propagation
queries checked against a naive independent propagator. Source tests cover altered resets,
wrong latency, missing enables, late failures, a property requiring `k=2`, initial-state freedom,
forged metadata/proofs, malformed witnesses and blocked SAT/SMT imports in a fresh process.

The 12-case synthetic source benchmark certifies four regular pipelines through 256 bits x 16
stages and requires specific rejection reasons for eight negative/incomplete cases. It retains
all timing samples, dependency versions, certificate sizes and source/request digests.
These are controlled capability measurements, not evidence of arbitrary 4,096-state-bit
machine performance or superiority over a proprietary tool. CI retains the actual test and
benchmark artifacts; timing numbers should be quoted with their workload and environment.

Method/API references: [PySAT solver API](https://pysathq.github.io/docs/html/api/solvers.html)
(DRUP logging and limited-call interruption), [DRAT-trim](https://www.cs.utexas.edu/~marijn/drat-trim/)
(RUP versus stronger RAT checking), and [Programming Z3](https://theory.stanford.edu/~nikolaj/programmingz3.html)
(section 5.4, bounded checking and induction). The bit blaster and RUP kernel here are independent
Python implementations of established methods, not a claimed new proof algorithm.

## Hierarchy and controllers

The receiver and producer now accept the documented elaborated hierarchy and controller subset
through the shared typed frontend. `always_comb`, ordinary first-match `case` and integral enums
are translated before CNF construction. The receiver always regenerates full-model obligations
and verifies full-state guard witnesses; Z3 cone reduction is not part of this certificate format.
Neither `verify-certificate` nor its controller/hierarchy imports needs a SAT/SMT solver.
Certificates from the earlier flat frontend must be regenerated because the bound IR includes
hierarchy and clock aliases. See [controller semantics](controller-verification.md).
