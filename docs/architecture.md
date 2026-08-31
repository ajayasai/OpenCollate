# Architecture

OpenCollate is organized around one rule: **parsers report observations; they do not decide
truth**.

## Pipeline

```text
source files
    │
    ▼
isolated parser adapters
    │  observations + provenance + fact state
    ▼
normalization
    │  names, roles, directions, shapes
    ▼
resolution and participation
    │  canonical object identities + explicit aliases
    ▼
design contract
    │
    ├── inventory checks
    ├── semantic checks
    ├── mapping checks
    └── completeness checks
            │
            ▼
diagnostics → text / JSON / Markdown / SARIF
```

Pairwise comparison grows quadratically and tends to hide which view supplied a conclusion.
OpenCollate instead reconciles every observation into one evidence-bearing contract. A new parser
does not need custom comparison logic for every existing parser.

## Layers

### Parser adapters

Each adapter owns syntax recovery and source-specific semantics. It emits components, ports,
shapes, roles, functions, mappings, and parser diagnostics in a parser-neutral model. An adapter
must not:

- Rename an object to make a mismatch disappear.
- Infer scalar width from a failed vector parse.
- Treat skipped data as absent data.
- Decide which source view is authoritative.

The SystemVerilog adapter uses pyslang; the Liberty, LEF, and CSV adapters are intentionally
isolated so they can evolve or be replaced without changing rules.

### Fact states

Every observation has a state:

- `known`: the adapter established a value.
- `unknown`: the source or context did not establish a value.
- `unsupported`: OpenCollate recognized a construct it cannot model.
- `tainted`: recovery produced data that may depend on an earlier parse problem.
- `not_applicable`: the property has no meaning for that object or view.

Only known facts may establish equality. Unknown, unsupported, and tainted facts remain visible
and cannot silently produce a pass.

### Normalization

Normalization converts representational variants—direction spelling, power roles, and vector
structure—without discarding the original spelling or source span. It is intentionally narrower
than aliasing.

### Resolution

Resolution groups observations into canonical component and port identities. Explicit aliases
handle intentional differences such as `VDD_CORE` versus `VDDC`. Participation policy answers
whether an object is expected in a view; it is separate from aliases so an omitted LEF pin cannot
be hidden by renaming.

### Contract and rules

The contract retains the observations used to derive each canonical property. Rules consume the
contract, not parser internals. This allows direct unit tests for reconciliation and rules and
makes `opencollate contract build` an audit surface.

### Diagnostics and reporters

Rules emit structured diagnostics. Renderers are pure transformations with stable ordering. A
renderer cannot change severity, evidence, waiver state, or exit status.

## Determinism

Inputs are keyed by declared view and normalized object identity rather than filesystem discovery
order. Contracts and diagnostics are sorted before serialization. Fingerprints derive from
semantic identity, not line number alone, so unrelated source movement does not churn waivers.

## Trust boundaries

Input files and configuration are untrusted. Parsers must avoid shell execution and never write
files. Only commands with an explicit output option may write, and output paths remain under user
control. See [SECURITY.md](../SECURITY.md).

## Package layout

```text
src/opencollate/
  cli.py               command surface
  config.py            TOML loading and validation
  model.py             parser-neutral facts and provenance
  normalize.py         value normalization
  resolve.py           aliases, identity, participation
  checks/              contract rules
  diagnostics.py       codes, fingerprints, waivers
  reporters/           text, JSON, Markdown, SARIF
  parsers/             format-specific adapters
```

The precise tree may evolve before 1.0; the layer boundaries are the compatibility goal.
