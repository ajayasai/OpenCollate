# OpenCollate

**Open, local-first consistency checking for heterogeneous SoC design collateral.**

OpenCollate compares structural facts across RTL/SystemVerilog, Liberty, LEF, DEF, experimental
GDSII, IP-XACT, SystemRDL, SDC, UPF, C register headers, package/pin CSV, CDL/SPICE, and declarative
connectivity intent. It reconciles those observations into canonical identities, retains source
provenance and fact state, and emits deterministic diagnostics for terminal, JSON, Markdown, and
SARIF consumers.

OpenCollate is deliberately not presented as a replacement for simulation, formal verification,
STA, CDC/RDC, DRC, LVS, synthesis, place-and-route, register generation, or tapeout signoff. Its
scope is pre-signoff collateral drift: contradictions, omissions, unresolved references, stale
interfaces, register-map disagreement, package-map inconsistency, and bounded static-connectivity
requirements that can be established from supported facts.

## Why OpenCollate

- **Local and auditable:** no account, telemetry, design upload, or license server.
- **Fail closed:** unknown, unsupported, tainted, incomplete, and not-applicable facts are distinct;
  parser or checker-plugin crashes cannot become a clean pass.
- **CI native:** deterministic text, JSON, Markdown, SARIF, report-diff, and contract artifacts.
- **Cross-view:** 13 collateral families feed one parser-neutral observation model.
- **Extensible:** versioned parser and semantic-checker APIs allow independently distributed rule
  packs and adapters without modifying the core.
- **Reproducible:** pinned CI actions, multi-platform testing, branch coverage, SBOMs, checksums, and
  release provenance.

## Install

OpenCollate requires Python 3.11 or newer.

```console
python -m pip install .
opencollate --version
```

For development:

```console
python -m pip install -e ".[dev]"
pytest
```

## First run

```console
opencollate demo
opencollate check examples/uart/opencollate.toml
```

The synthetic UART intentionally contains inconsistencies, so `check` exits with status 1 after a
trustworthy analysis. Use `--format json`, `--format markdown`, or `--format sarif` for machine and
review workflows.

## Supported collateral

| Collateral | Current supported surface |
| --- | --- |
| Verilog/SystemVerilog | modules, hierarchy, ports, parameters, elaborated shapes, selected Boolean functions, transparent static connectivity |
| Liberty | cells, pins, direction, role, selected Boolean functions; timing/noise/power tables are not analysed |
| LEF | macros, pins, direction/use, selected structural references; geometry is not analysed |
| DEF | design/component/net/pin references and bounded structural mappings; routing geometry is not analysed |
| GDSII | streaming cell/reference/text structure and explicitly filtered pin labels; polygon geometry is not analysed |
| IP-XACT | 2009/2014/2022 components, ports, interfaces, parameters, memory maps, registers, fields |
| SystemRDL | selected 2.0 structural register facts, nested maps/files, arrays, access and reset metadata |
| SDC | safe static Tcl subset for clocks, I/O delays, timing exceptions, groups, and object references |
| UPF | safe static Tcl subset for power domains, supplies, switches, isolation, retention, level shifters, and references |
| C headers | conventional bounded integer register macros and fields |
| Package/pin CSV | component-pin and package-map profiles, exploded buses, pad/ball/signal consistency |
| CDL/SPICE | subcircuits, ordered terminals, devices and selected structural references; no simulation |
| Connectivity CSV | required/forbidden bounded static paths, transforms, waypoints, exclusions, witness/cut reporting |

The exact parser and checker capability inventory for an installed environment is available with:

```console
opencollate capabilities --json
```

## Fact states and trustworthy absence

Parsers emit observations rather than truth. Every fact can be `known`, `unknown`, `unsupported`,
`tainted`, or `not_applicable`. Incomplete parsing therefore cannot silently erase a mismatch.
Diagnostics distinguish a demonstrated contradiction from a comparison that could not be
completed. A rule that lacks enough applicable evidence produces an explicit inconclusive or
not-applicable diagnostic; absence of a mismatch is not proof of equivalence.

## Commands

```text
opencollate check [CONFIG]                  # default: opencollate.toml
opencollate review [CONFIG] --baseline REPORT
opencollate report diff BASELINE CURRENT
opencollate check -c path/to/config.toml
opencollate demo [--output-dir DIR]
opencollate init [PATH]
opencollate capabilities [--json]
opencollate explain CODE
opencollate schema [report|contract|diff] [--output PATH]
opencollate contract build [CONFIG] --output contract.oc.json
opencollate contract migrate LEGACY --output contract.v2.oc.json
```

| Status | Meaning |
| ---: | --- |
| 0 | The command completed and no unwaived error-level violations were found |
| 1 | The check completed and found unwaived violations |
| 2 | Configuration, input, parser, output, or internal failure prevented a trustworthy result |

`demo` returns 0 by default because its inconsistencies are intentional; use `demo --strict-exit`
to propagate its check status. Read [exit codes](docs/exit-codes.md) before CI integration.
Use [baseline review](docs/baseline-review.md) to gate only new or changed findings while retaining
fatal-analysis semantics.

## Design contract, not pairwise spaghetti

Each importer emits parser-neutral observations with source provenance and fact state. Resolution
groups them into canonical identities; rules consume those identities and retain evidence from
every view.

```text
RTL / Liberty / LEF / CDL / DEF / GDSII / IP-XACT / SystemRDL
                    │
CSV / connectivity intent / SDC / UPF / C headers
                    ▼
        observations + provenance
                    ▼
     canonical components, ports, registers
                    ▼
 rules → terminal / JSON / Markdown / SARIF / contract JSON
```

Inspect the exact contract being checked:

```console
opencollate contract build examples/uart/opencollate.toml --output contract.oc.json
```

Newly generated schema-version-2 contracts persist canonical components, ports, and registers plus
integrity-checked snapshots of every parser-neutral observation family: clocks, interfaces,
hierarchy and references, package mappings, constraint/power metadata, registers, connectivity,
view attributes, completeness, and tainted scopes. Version-1 contracts remain readable and can be
migrated without inventing facts they never stored. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).

## Versioned extension platform

Installed packages can add collateral parsers through the `opencollate.parsers` entry-point group
and semantic checks through `opencollate.checkers`. Registrations declare extension API version 1,
provider/version provenance, aliases, and filename suffixes. They cannot silently shadow built-in
formats. Parser crashes become fatal, whole-view-tainted `OC9001` observations; checker discovery
or execution crashes become fatal `OC9002` diagnostics.

```console
opencollate capabilities --json  # exact built-in/plugin ownership and failures
```

See the [extension API](docs/extension-api.md) for packaging, runtime registration, compatibility,
configuration forwarding, deterministic conflict handling, and the plugin trust boundary. Set
`OPENCOLLATE_DISABLE_PLUGINS=1` when a hermetic run must ignore installed entry points.

## Security and privacy

OpenCollate treats configuration and collateral as untrusted input. SDC and UPF are tokenized as
static text; Tcl is never started. C macro and IP-XACT integer expressions use small bounded
evaluators, not compilers or `eval`. IP-XACT rejects DTD/entity declarations and never fetches a
schema. SystemRDL Perl tags/includes are rejected before compilation, and connectivity intent is
bounded declarative data rather than a property language. CDL/SPICE is never simulated; DEF/LEF
geometry is skipped structurally; and GDSII geometry records are bounded and discarded without
polygon construction.

OpenCollate has no telemetry, account, upload, or network-reporting feature. Reports and contracts
still contain design names, paths, connectivity, expressions, and normalized parser metadata and
must be protected like source collateral. Read the [security model](docs/security-model.md) and
[privacy statement](docs/privacy.md).

## No-signoff positioning

A clean OpenCollate report means only that enabled rules found no contradiction in facts they
could establish. It does not certify correctness, completeness, manufacturability, timing,
electrical behavior, power intent, or tapeout readiness. Review `OC1102`–`OC1105`, unknowns,
tainted scopes, configuration participation, and parser coverage before relying on any result.

## Project

- [Roadmap](ROADMAP.md)
- [Competitive evidence](docs/competitive-scorecard.md)
- [Public benchmarks](benchmarks/README.md)
- [Contributing](CONTRIBUTING.md)
- [Governance](GOVERNANCE.md)
- [Support](SUPPORT.md)
