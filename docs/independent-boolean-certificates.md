# Independently checkable Boolean certificates

This development feature separates **finding an answer** from **checking its logical evidence**.
`formal certify` optionally uses Glucose3 through `python-sat` to produce evidence.
`formal verify-certificate` does not import or invoke a SAT/SMT solver. It reconstructs the
actual Boolean problem from the supplied request and checks every proof inference or witness.
The normal RTL/Liberty consistency engine can explicitly select the same checked backend.

This is a two-valued **combinational** feature. It does not upgrade the independent-proof status
of `sequential check`, accept full SystemVerilog/SVA, or establish tapeout signoff. The previous
`formal check` / `formal replay` commands remain available; replay still means repeated solving,
not certificate checking.

## Run it

Install from this development checkout, not the older `v0.3.0` tag:

```console
python -m pip install -e ".[certificates]"
opencollate formal certify examples/formal/certified-obligations.json --output certificate.json
opencollate formal verify-certificate examples/formal/certified-obligations.json certificate.json
opencollate schema formal-certificate --output formal-certificate.schema.json
```

The verification result includes `solver_invoked: false`, the request and certificate digests,
the number of logically checked results, the aggregate outcome, and the exit code. A recipient
needs the request and certificate plus the base OpenCollate package; neither `python-sat` nor Z3
is required for verification. No account, license server, network fetch, executable solver script,
or private design upload is used by the checker.

For normal collateral checks, install the producer extra and choose:

```toml
[policy]
boolean_backend = "certified"
max_symbolic_inputs = 512
symbolic_timeout_ms = 5000
symbolic_resource_limit = 100000
```

The last setting bounds native conflicts **per query** for this backend, not Z3's resource units.
The engine feeds its parsed RTL/Liberty Boolean IR into generation and independently checks the
returned evidence before accepting equivalence. Mismatches still produce `OC4301` with a complete
counterexample. Missing dependencies, failed proof checking, unsupported declared function syntax,
or exhausted budgets produce fatal `OC4302` and exit 2. The default `truth_table` backend and its
existing warning-level limits are unchanged. The `z3` backend now also preserves unsupported,
explicitly declared Liberty function text for an incomplete-analysis diagnostic instead of losing
it when the parser omits the corresponding known Boolean fact.

Normal `check` reports do not export a portable certificate for every clean source-level match.
The standalone certificate commands bind the submitted formulas and assumptions, **not the RTL
source bytes or the correctness of a user's translation**. Keep that distinction when sharing
certificates. The engine's source-linked diagnostic evidence is not a supplier signature either.

## Exactly what is established

Let the request contain left function `L`, right function `R`, and assumption `A`.

| Result | Required evidence | Aggregate effect |
| --- | --- | --- |
| `equivalent` | A complete Boolean assignment satisfying `A`, plus a checked refutation of `A AND (L XOR R)` | Can pass |
| `different` | Complete Boolean assignments replaying satisfiable assumptions and an actual guarded mismatch | Exit 1 unless incomplete evidence is also present |
| `vacuous` | A checked refutation of `A`; no satisfying witness is asserted | Exit 2, never an equivalence pass |
| `inconclusive` | No logical evidence is accepted; a bounded reason is retained | Exit 2 |

False-before-true refinement in sorted source-variable order makes witnesses canonical for a
fixed problem. The witness evaluator is independent of native solver models and checks exact
Boolean values: `0`, `1`, and strings are not accepted as JSON Boolean assignments.

## Proof construction and checking

`proof_cnf.py` validates the supported Boolean IR, applies explicit variable aliases, assigns
stable input literals, and builds a deterministic Tseitin encoding of AND, OR, NOT, XOR and
constants. The verifier repeats this translation from the actual request. It never accepts a
certificate-supplied base CNF as authoritative. Empty reductions, finite traversal, cyclic-IR
rejection, and constant/complement simplifications are covered by tests.

The producer uses a fresh Glucose3 instance for each base query. UNSAT proofs are generated
without solver assumptions attached to that query. Complete SAT models must satisfy the actual
CNF before they can become source witnesses. Native errors, `SystemExit`, missing models,
contradictory literals, unknown statuses and invalid traces cannot establish a pass.

`boolean_proof_kernel.py` checks an addition-only **reverse-unit-propagation (RUP)** proof. For each added
clause, it temporarily negates every literal and requires unit propagation against the actual
base formula and previously checked clauses to reach a contradiction. The final clause must be
explicitly empty, with no trailing steps. Every nonempty addition is checked too; a matching
checksum or an UNSAT status is not a proof.

Native deletion instructions are ignored while normalizing the producer's DRUP trace: already
justified clauses remain available. This is sound for the accepted RUP subset. RAT-only additions,
new variables outside the reconstructed formula, tautological/duplicate certificate literals,
zero literals, and unsupported trace commands are not accepted as proof inferences. If native
root propagation emits no trace, the producer proposes an empty clause; it still has to pass the
same logical checker.

The kernel tracks remaining literal counts so wide clauses are not rescanned after every false
literal. This is an inspectable Python implementation, **not a mechanically verified kernel**.

## Binding, limits, and trust

The versioned bundle binds the normalized request, the Boolean obligation, the deterministic
CNF, and the full certificate content using SHA-256. Recomputing hashes does not authorize false
proofs: verification still checks the reconstructed logic. These hashes provide integrity/binding,
not identity, authentication, an external timestamp, or cryptographic supplier approval.

The trusted computing base still includes the Boolean parser, alias/IR validation, Tseitin
translation, witness evaluator, RUP kernel and Python runtime. The producer's SAT/UNSAT answer is
not trusted by itself. The finite tests and an external checker cross-check reduce implementation
risk but are not a mathematical proof of the implementation's correctness.

The public CLI exposes `--max-variables`, `--timeout-ms`, `--resource-limit`,
`--max-proof-steps` and `--max-proof-work`. `ProofLimits` additionally bounds IR nodes,
clauses and stored literals. Defaults are 512 source variables, 32,768 IR nodes, 131,072 retained
clauses, 1,048,576 stored literals, 32,768 proof steps, and 20,000,000 counted verification work
units. The CLI's default native conflict budget is 1,000,000 per query; the Python API's default
is 100,000. All numerical controls reject Boolean, zero, negative and over-ceiling values.

The time budget applies to each obligation, not to an entire 128-obligation bundle. A timer
interrupts native solving; verifier work is bounded separately and budget exhaustion is not an
accepted result. Native construction and proof extraction are not a process-level security
sandbox or a hard allocation limit. Use `opencollate guard` for a wall-time/process ceiling:

```console
opencollate guard --wall-seconds 60 -- formal certify examples/formal/certified-obligations.json --output certificate.json
```

Inputs and outputs use the existing bounded, duplicate-key-rejecting JSON reader and atomic
publication. Request/certificate output aliases, including supported symlink and hardlink aliases,
are rejected. Certificates contain variable names, assumptions and witnesses; protect them like
source collateral.

## Reproducible evidence

```console
python -m pip install -e ".[dev]"
pytest -q tests/test_boolean_proof_kernel.py tests/test_proof_cnf.py tests/test_certificates.py tests/test_certificate_integration.py tests/test_certificate_benchmarks.py
python -m benchmarks.boolean_certificates --repeat 5 --json-output certificates.json --export-dir certificate-evidence
```

The public corpus has 15 expected outcomes: paired De Morgan controls/mutants at 12, 64, 128 and
512 inputs; guarded mux cases; contradictory assumptions; a constant mismatch; nontrivial parity
permutation; unsupported syntax; and deliberately exhausted verification work. Eight poisoned
certificate controls recompute checksums and must still be rejected. Each normal case is repeated
with outcome, digest, proof size, raw timing samples and producer environment retained. These are
synthetic conformance measurements, not evidence of industrial scale or superiority over a
commercial tool. Proof bytes need not remain identical across different producer versions.

Tests also exhaustively compare all 512 CNFs over two variables with truth tables, exercise
random three-variable formulas/proof candidates, compare CNF semantics and generated certificates
with exhaustive expression evaluation, inject native model/status failures, and verify certificates
in a subprocess that forbids SAT/SMT imports. Actual 64-input RTL/Liberty files exercise engine
integration and the unsupported-function false-pass regression.

The `Independent Boolean certificates` workflow builds revision-pinned
[DRAT-trim](https://github.com/marijnheule/drat-trim/tree/2e3b2dc0ecf938addbd779d42877b6ed69d9a985)
and checks all seven exported UNSAT CNF/proof pairs using a separate native implementation.
It also verifies an installed wheel without either solver extra. Read the actual workflow outcome
and retained checker logs before claiming those remote checks passed for a given commit.
