# OpenCollate

[![CI](https://github.com/ajayasai/OpenCollate/actions/workflows/ci.yml/badge.svg)](https://github.com/ajayasai/OpenCollate/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Catch SoC collateral drift before tapeout.**

OpenCollate builds one canonical design contract from RTL and implementation collateral, then
explains every disagreement in design language. It is local-first, scriptable, and open source.

```text
ERROR OC4001  uart/irq_o has conflicting directions: RTL, LEF (abstract) = output; Liberty (tt) = input.
  --> rtl/uart.sv:8:24 [rtl.default] = output
  --> lef/uart.lef:57:3 [lef.abstract] = output
  --> lib/uart.lib:39:9 [liberty.tt] = input
  help: Correct the direction in the outlying collateral or the canonical contract.
```

A width diagnostic is equally direct: `uart/irq is 1 bit in RTL but 4 bits in Liberty.`

> **Alpha software:** OpenCollate 0.1.0 is useful for evaluation and CI experiments. Its Python
> API, contract schema, rule set, and diagnostic fingerprints may change before 1.0. It is not a
> replacement for signoff verification.

## Install

OpenCollate requires Python 3.11 or newer.

```console
python -m pip install "git+https://github.com/ajayasai/OpenCollate.git@v0.1.0"
opencollate --version
```

OpenCollate is not yet published on PyPI. To work from a checkout:

```console
python -m pip install -e ".[dev]"
```

## Thirty-second tour

Run a generated, synthetic demonstration without providing design files:

```console
opencollate demo
```

Or check the repository's deliberately inconsistent UART example:

```console
opencollate check examples/uart/opencollate.toml
```

The default configuration name is `opencollate.toml`, so this also works from the example
directory:

```console
cd examples/uart
opencollate check
```

OpenCollate's diagnostics carry a stable code, the affected object and property, evidence from
each view, source locations where available, and a remediation hint. Terminal prose is for
people; JSON and SARIF are available for automation and code-scanning integrations.

## What the Alpha understands

| View | 0.1 support | Deliberate boundary |
| --- | --- | --- |
| Verilog/SystemVerilog | Modules, ANSI and non-ANSI ports, directions, evaluated packed and unpacked dimensions, includes, defines, source spans, and top selection through pyslang | Interfaces, `ref` ports, and unevaluable shapes are reported as unsupported or unknown; they are never guessed as scalars |
| Liberty | Libraries, cells, pins, buses, bundles, types, `pg_pin`, direction, use, bus ranges, and small Boolean functions | Timing and power table contents are skipped, not interpreted |
| LEF | Version and naming directives plus macro/pin, direction, and use data | Geometry and vendor properties are tolerated but do not participate in checks |
| CSV pin maps | UTF-8 BOM and RFC 4180 CSV, header aliases, scalar/range/per-bit rows, direction, and role normalization | Vendor-specific columns require explicit header mapping |
| IP-XACT, SDC, UPF | Planned | Not accepted by 0.1 |

See [supported syntax](docs/supported-syntax.md) for exact constructs and unknown-state behavior.
The machine-readable state is always **known**, **unknown**, **unsupported**, **tainted**, or
**not applicable**; parser recovery never silently invents design facts.

## Checks in the first release

- Module, macro, cell, and pin inventory differences.
- Port/pin name, direction, width, range, and ordering differences.
- Missing or extra power and ground pins.
- Clock and reset role inconsistencies when roles are present in the imported views.
- Liberty Boolean function versus small RTL expression equivalence.
- Die-pad and package-pin mapping consistency from CSV.

Boolean equivalence is exact truth-table comparison for expressions within the configured input
limit (12 variables by default). Larger or unsupported expressions produce an explicit
inconclusive diagnostic instead of a false pass.

## Commands

```text
opencollate check [CONFIG]                  # default: opencollate.toml
opencollate check -c path/to/config.toml
opencollate demo [--output-dir DIR]
opencollate init [PATH]
opencollate capabilities
opencollate explain CODE
opencollate schema [report|contract] [--output PATH]
opencollate contract build [CONFIG] --output contract.oc.json
```

Exit status is part of the CLI contract:

| Status | Meaning |
| ---: | --- |
| 0 | The command completed and no unwaived error-level violations were found |
| 1 | The check completed and found unwaived violations |
| 2 | Configuration, input, parser, or internal failure prevented a trustworthy result |

`demo` returns 0 by default because its inconsistencies are intentional; use `demo --strict-exit`
to propagate the demonstration check status.

See [exit codes](docs/exit-codes.md) before integrating OpenCollate into CI.

## Design contract, not pairwise spaghetti

Each importer converts a source view into the same evidence-bearing model. Normalization and
explicit aliases resolve representational differences. Checks compare reconciled facts and emit
diagnostics that retain provenance back to every source.

```text
Verilog ─┐
Liberty ─┼─> facts + provenance ─> canonical contract ─> rules ─> terminal / JSON / SARIF
LEF ─────┤
CSV ─────┘
```

This structure keeps parsers replaceable, makes rule behavior testable without a parser, and
allows teams to inspect the exact contract being checked:

```console
opencollate contract build --output contract.oc.json
```

Read [the architecture](docs/architecture.md), [canonical contract](docs/canonical-contract.md),
[diagnostic model](docs/diagnostics.md), and [rule catalog](docs/rule-catalog.md).

## Configuration

Start with a documented configuration:

```console
opencollate init .
```

The generated `opencollate.toml` provides starter source views, a reference baseline, and core
policy settings. Project-specific aliases, participation rules, and waivers are added separately.
Paths are resolved relative to the configuration file. Read the
[configuration guide](docs/configuration.md) and see the synthetic
[UART configuration](examples/uart/opencollate.toml).

## Privacy and reproducibility

OpenCollate runs locally. It contains no telemetry, account, upload, or network-reporting feature.
The files you check stay on the machine unless you deliberately send reports elsewhere. Output is
deterministically ordered to make code review and CI diffs useful. Read the
[privacy statement](docs/privacy.md).

## Non-goals

OpenCollate does not synthesize RTL, perform timing analysis, prove arbitrary sequential logic,
run DRC/LVS, or certify a design for tapeout. A clean report only means that the enabled rules
found no contradictions in the facts they could establish. Unknown and unsupported facts must be
reviewed.

## Project

- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Governance](GOVERNANCE.md)
- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

OpenCollate is licensed under the [Apache License 2.0](LICENSE).
