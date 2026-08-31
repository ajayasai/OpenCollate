# Architecture

OpenCollate is organized around one rule: **parsers report observations; they do not decide
truth**.

## Pipeline

```text
source files
    │
    ▼
format-specific, non-writing parser adapters
    │  observations + provenance + fact state + parser diagnostics
    ▼
normalization and identity resolution
    │  names, roles, directions, shapes, aliases, participation
    ▼
canonical design and optional frozen contract
    │
    ├── interface and inventory checks
    ├── object-reference and clock checks
    ├── mapping and register checks
    ├── bounded static connectivity checks
    └── completeness and integrity checks
            │
            ▼
diagnostics → text / JSON / Markdown / SARIF / contract JSON
```

Pairwise comparison grows quadratically and hides which view supplied a conclusion. OpenCollate
instead reconciles observations into shared identities while preserving every source spelling,
view, and location. A new parser can contribute facts without custom comparisons against every
existing parser.

## Observation model

`ViewObservation` is the parser boundary. It can carry:

- Component and port definitions, shapes, roles, directions, and small Boolean functions.
- Explicit die-pad/package-ball/signal mappings.
- Named design-object definitions or references with kind, scope, and relation.
- Static clock definitions and targets.
- IP-XACT interfaces and logical-to-physical port maps.
- Registers and fields with address, size, layout, access, and reset facts.
- Bit-level transparent RTL connectivity edges and declarative required/forbidden path intent.
- Experimental GDSII cell definitions, SREF/AREF hierarchy references, and text-label objects;
  selected labels can supply unknown-shape ports only under explicit filters.
- Parser diagnostics, whole-view completeness, tainted scopes, and source-specific attributes.

This distinction matters. A DEF placed component is a design-object instance, not another top
component interface. An SDC `get_ports` result is a reference to resolve, not a port definition.
A standard DEF pin/net does not establish a package mapping.

## Fact states

Every observation or field is `known`, `unknown`, `unsupported`, `tainted`, or
`not_applicable`. Only known values can establish equality. Recovery data remains visible, but a
tainted width or dynamic Tcl reference cannot silently create a pass or an absent-object error.

## Parser trust boundary

Parsers consume untrusted text and do not write source files. The important execution boundaries
are:

- SystemVerilog is parsed and elaborated by pyslang; it is not simulated.
- Connectivity extraction stops at procedural, dynamic, or non-transparent logic and records an
  inconclusive frontier rather than assuming reachability or isolation.
- SDC and UPF use static Tcl-shaped tokenizers and never start Tcl or execute commands.
- C-header and IP-XACT integer expressions use bounded, side-effect-free evaluators.
- IP-XACT rejects DTD/entity declarations, fetches no schemas, and expands no external definitions.
- CDL/SPICE is structurally tokenized, never simulated, and parameters remain text.
- DEF and LEF geometry is structurally skipped rather than interpreted as names or connectivity.
- GDSII is parsed as a bounded native record stream; geometry elements are counted and discarded
  without polygon materialization or physical verification.
- SystemRDL executable preprocessing and includes are rejected before bounded compilation through
  `systemrdl-compiler`.

Parsers with explicit size, token, nesting, and object limits fail closed. See
[supported syntax](supported-syntax.md) and the [security model](security-model.md).

## Normalization, aliases, and participation

Normalization converts representational variants—direction spelling, role spelling, and vector
structure—without discarding the original token or source span. Aliases explicitly group
different native names under one canonical identity. Participation states which views are
expected to contain an object.

These mechanisms are intentionally separate: an alias cannot hide absence, participation cannot
rewrite a conflicting value, and neither mechanism turns unknown evidence into known evidence.

## Contract and runtime overlays

Schema version 1 of the frozen contract persists canonical components, ports, and registers. The
runtime observation graph also contains clocks, interfaces, constraints, hierarchy references,
UPF objects, DEF connectivity, and pin mappings. Rules evaluate those overlays during a check,
but they are not all serialized as frozen-contract authorities in 0.3.0. Connectivity graph and
intent facts are deliberately run-local in contract schema version 1.

The contract retains the selected canonical values and per-view native names. Diagnostics retain
the observation evidence used by a rule. Build and inspect a contract with:

```console
opencollate contract build opencollate.toml --output contract.oc.json
```

## Rules and reporters

Rules consume parser-neutral observations and canonical identities, not parser internals. The
runtime catalog is the authority for code, default severity, summary, and remediation. Reporters
are pure transformations: they cannot change severity, waiver state, fingerprint, evidence, or
exit status.

Saved report review is a separate pure comparison. The diagnostic fingerprint identifies an issue;
a full semantic content digest detects changes while ignoring source movement and evidence order.
Fatal current findings cannot be ratcheted away.

## Determinism

Views and files are ordered deterministically. Contracts and diagnostics are sorted before
serialization. Fingerprints derive from semantic identity rather than a line number alone, so
unrelated source movement does not intentionally churn waivers. Determinism supports reviewable
diffs; it does not anonymize design data.

## Package layout

```text
src/opencollate/
  cli.py               command surface and parser dispatch
  config.py            TOML loading and validation
  model.py             parser-neutral facts, contracts, and provenance
  engine.py            reconciliation and built-in checks
  baseline.py          deterministic saved-report multiset comparison
  catalog.py           authoritative rule registry
  diagnostics.py       findings, fingerprints, and waivers
  reporters/           text, JSON, Markdown, and SARIF
  parsers/             format-specific adapters
  schemas/             report, contract, and report-diff JSON Schemas
```

The precise tree can evolve before 1.0; parser neutrality, explicit fact state, provenance, and
non-executing treatment of executable-shaped collateral are the compatibility goals.
