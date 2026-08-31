# OpenCollate

[![CI](https://github.com/ajayasai/OpenCollate/actions/workflows/ci.yml/badge.svg)](https://github.com/ajayasai/OpenCollate/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Catch SoC collateral drift before tapeout.**

OpenCollate builds one provenance-preserving design contract from RTL, implementation,
constraints, power intent, register descriptions, circuit netlists, and package collateral. It
then reports contradictions in design language. It is local-first, scriptable, and open source.

```text
ERROR OC4001  uart/irq_o has conflicting directions: RTL, CDL, DEF, IP-XACT, LEF = output; Liberty = input.
  --> rtl/uart.sv:8:24 [rtl.default] = output
  --> lib/uart.lib:39:9 [liberty.tt] = input
  help: Correct the direction in the outlying collateral or the canonical contract.
```

> **Beta software:** OpenCollate 0.3.0 is intended for evaluation, collateral review, and CI
> experiments. Its Python API, schema, and diagnostic surface can still change before 1.0. It is
> not a signoff tool and does not replace simulation, formal verification, STA, CDC/RDC, DRC,
> LVS, extraction, or implementation-tool validation.

## Install

OpenCollate requires Python 3.11 or newer.

```console
python -m pip install "git+https://github.com/ajayasai/OpenCollate.git@v0.3.0"
opencollate --version
```

OpenCollate is not yet published on PyPI. To work from a checkout:

```console
python -m pip install -e ".[dev]"
```

## Thirty-second tour

Run the generated demonstration:

```console
opencollate demo
```

Or run the repository’s synthetic, deliberately inconsistent UART across all thirteen supported view
kinds:

```console
opencollate check examples/uart/opencollate.toml
```

The full-stack example imports SystemVerilog, Liberty, LEF, CSV, IP-XACT, SystemRDL, declarative
connectivity intent, SDC, UPF, a C register header, CDL, DEF, and experimental structural GDSII. It is expected to return status 1 with
exactly `OC4001`, `OC4301`, and `OC5003`. See the
[UART walkthrough](examples/uart/README.md).

## 0.3.0 support matrix

“Structural” means OpenCollate imports facts needed for consistency checks; it does not implement
the complete language or the analysis normally performed by its native tool.

| View | 0.3.0 Beta support | Deliberate boundary |
| --- | --- | --- |
| Verilog/SystemVerilog | pyslang preprocessing, parsing, and elaboration; modules, ports, parameters, dimensions, includes, defines, hierarchy, and small continuous-assignment Boolean functions | No behavioral or sequential equivalence; unsupported ports and unresolved shapes remain explicit |
| Liberty | Libraries, cells, pins, buses, bundles, types, `pg_pin`, roles, directions, ranges, and small Boolean functions | Timing, power, noise, and characterization table contents are skipped |
| LEF | Macro/pin interface, direction, use, and declared bus naming | Geometry, vias, obstructions, and antenna/vendor properties are skipped |
| CSV | Component-pin and package-map profiles, configurable columns, ranges/per-bit rows, roles, directions, pads, balls, and signals | It is not a general spreadsheet importer; ambiguous identity columns must be mapped |
| IP-XACT | IEEE 1685 2009/2014/2022 components, ports, vectors/arrays, parameters, interfaces/port maps, memory maps, registers, and fields | No XSD validation, schema fetching, or external definition expansion |
| SystemRDL 2.0 | Ordered explicit units through `systemrdl-compiler`; selected top, nested maps/regfiles, arrays, addresses, widths, access, fields, and resets | Perl preprocessing and source includes are rejected; no RTL/UVM/software/document generation or behavioral verification |
| Connectivity CSV | Required/forbidden transparent RTL paths across assignments, hierarchy, net aliases, and simple primitives, with collision-free escaped names, bit identity/reversal, known inversion, waypoints/exclusions, witnesses, cuts, and fail-closed tainted frontiers | Bounded static graph analysis only; no temporal, conditional, sequential, mode-aware, or formal proof |
| SDC | Non-executing Tcl tokenizer; static queries, clocks, generated clocks, I/O delays, false paths, and multicycle paths | Tcl control flow, arbitrary commands, and environment-dependent substitution are not executed |
| UPF | Non-executing structural subset for design/scope, domains, supplies, isolation, retention, level shifting, switches, and power states | It is not a UPF interpreter or power-intent signoff engine; dynamic Tcl is not executed |
| C register headers | Conventional base/address/offset and field position/mask/width/reset integer macros | No C preprocessor, conditional-build selection, compiler, or arbitrary macro execution |
| CDL/SPICE | Subcircuits, explicit pin metadata, connectivity, globals, models, M/R/C/L/X structure, and continuations | No simulation, parameter evaluation, device-model validation, or electrical equivalence |
| DEF 5.8 | `DESIGN`, `COMPONENTS`, `PINS`, `NETS`, `SPECIALNETS`, placement, connectivity, hierarchy, and bus naming | Routes and geometry are skipped; DEF pins do not imply package-ball/die-pad mappings |
| GDSII (experimental) | Bounded native big-endian stream parsing for cells, inferred/selected tops, SREF/AREF hierarchy, transforms, and text labels; selected labels can become unknown-shape ports only through explicit layer/type filters | Polygon/path/node/box geometry is never materialized or verified; no DRC, LVS, extraction, connectivity inference, or implicit label-to-pin guessing |

Read the [exact supported syntax and limits](docs/supported-syntax.md). The installed build is
authoritative:

```console
opencollate capabilities
```

Every fact is **known**, **unknown**, **unsupported**, **tainted**, or **not applicable**. Parser
recovery never turns an unestablished fact into an apparent pass.

## What OpenCollate checks

The 74-rule 0.3.0 catalog covers:

- Configuration, parser completeness, unsupported constructs, and tainted scopes.
- Component and port identity, inventory, direction, role, shape, range, and ordering.
- Small combinational RTL/Liberty Boolean equivalence.
- Die-pad, package-ball, and logical-signal mappings from explicit mapping sources.
- SDC objects and clocks against statically elaborated RTL.
- UPF object references and duplicate or missing power-intent objects.
- IP-XACT interface-to-physical-port maps.
- IP-XACT, SystemRDL, and C-header register addresses, widths, fields, access, resets, and layout.
- Declarative required/forbidden RTL connectivity, width, bit ordering, known polarity, waypoints,
  and excluded nodes inside the transparent static subset.
- DEF endpoints against the elaborated RTL hierarchy.
- GDSII structure and explicitly selected text-label port inventory through the common component
  contract; geometry is not checked.

See the [rule catalog](docs/rule-catalog.md). Inconclusive evidence produces a completeness or
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

The frozen contract currently persists components, ports, and register maps. Clocks, interfaces,
hierarchical objects, constraints, and mappings remain first-class run observations but are not
all frozen-contract fields in schema version 1. Read the [architecture](docs/architecture.md),
[canonical contract](docs/canonical-contract.md), and [diagnostic model](docs/diagnostics.md).

## Security and privacy

OpenCollate treats configuration and collateral as untrusted input. SDC and UPF are tokenized as
static text; Tcl is never started. C macro and IP-XACT integer expressions use small bounded
evaluators, not compilers or `eval`. IP-XACT rejects DTD/entity declarations and never fetches a
schema. SystemRDL Perl tags/includes are rejected before compilation, and connectivity intent is
bounded declarative data rather than a property language. CDL/SPICE is never simulated; DEF/LEF geometry is skipped structurally; and GDSII
geometry records are bounded and discarded without polygon construction.

OpenCollate has no telemetry, account, upload, or network-reporting feature. Reports still contain
design names, paths, connectivity, and expressions and must be protected like source collateral.
Read the [security model](docs/security-model.md) and [privacy statement](docs/privacy.md).

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
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

OpenCollate is licensed under the [Apache License 2.0](LICENSE).
