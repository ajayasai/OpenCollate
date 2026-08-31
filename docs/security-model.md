# Security model

OpenCollate treats project configuration and every collateral file as untrusted input. Its parser
boundary is designed for static extraction: source text supplies observations, never commands to
run. This document describes 0.2.0 behavior; it is not a claim that arbitrary resource-exhaustion
attacks are impossible.

## Execution boundary

| Input | What OpenCollate does | What it does not do |
| --- | --- | --- |
| Verilog/SystemVerilog | Preprocesses, parses, and elaborates through pyslang | Simulate, synthesize, invoke a simulator, or run shell commands |
| Liberty/LEF/CSV/DEF | Tokenizes or structurally parses local text | Execute vendor extensions or interpret geometry as code |
| GDSII (experimental) | Validates a bounded native big-endian record stream and retains cells, hierarchy, and text labels | Materialize polygons, infer ports without explicit text filters, verify geometry, run DRC/LVS, or extract connectivity |
| IP-XACT | Parses local XML with namespace processing and bounded integer evaluation | Fetch schemas, resolve external definitions, accept DTD/entities, or run embedded content |
| SDC | Tokenizes a documented static Tcl subset | Start Tcl, run command substitution outside the subset, source files, or access the environment |
| UPF | Tokenizes a documented static Tcl-shaped subset | Interpret arbitrary Tcl, execute procedures/control flow, or reproduce a power tool |
| C header | Reads object-like integer macros and evaluates a small AST allow-list | Run a C preprocessor/compiler, follow includes, select conditional branches, or use `eval` |
| CDL/SPICE | Extracts structural subcircuits, pins, instances, nets, and metadata | Simulate, evaluate parameters, load device models, or invoke a simulator |

Unexpected or dynamic constructs produce `OC1101`–`OC1105` and taint dependent facts instead of
being executed or guessed.

## XML safety

IP-XACT uses a local streaming XML parser. DTD and entity declarations are rejected. The importer
does not perform XSD validation, XInclude, network access, catalog resolution, or external
definition expansion. Unsupported namespaces and external definition references are visible as
unsupported/tainted evidence.

## Static expression evaluators

The IP-XACT and C-header evaluators accept integer literals, named values, and an explicit
operator allow-list. They build and walk bounded syntax trees; they do not call Python `eval`, a
compiler, Tcl, or a shell. Expression size, nesting/node count, macro recursion, shift counts, and
integer bit length are limited. Division by zero, cycles, unknown names, and unsupported operators
produce controlled diagnostics.

## Resource controls

IP-XACT, SDC, C-header, CDL/SPICE, GDSII, Liberty Boolean expressions, and DEF publish format-specific
limits for decoded input, tokens/elements, nesting, expressions, or emitted objects. Limit
breaches fail closed and taint or reject the affected view. Exact values are listed in
[supported syntax](supported-syntax.md).

The 0.2.0 UPF tokenizer does not expose the same explicit aggregate caps as SDC. For adversarial
or very large collateral, run OpenCollate inside the organization’s standard job sandbox with
memory, CPU-time, file-size, and workspace access limits. Parser limits complement operating-system
isolation; they do not replace it.

## Filesystem and network behavior

Normal checks read the configured local files and do not upload data. OpenCollate has no account,
telemetry, license-server, or network-reporting feature. Commands write only when an output path
or output-producing operation is requested, such as `--output`, schema generation, contract
build, `init`, or demo output.

Configuration controls the local paths OpenCollate can read. Run untrusted configurations with a
filesystem sandbox that exposes only the intended workspace. CI upload steps, package installation,
and code-scanning services are outside OpenCollate and follow their own network/security policy.

## Sensitive outputs

JSON, SARIF, Markdown, terminal logs, and frozen contracts can contain design names, source paths,
source spans, expressions, hierarchy, register addresses, package connectivity, and waiver text.
Deterministic output is not anonymized or encrypted. Store and transmit reports under the same
controls as source collateral.

## Extension-code boundary

Built-in parsers follow this static model. The `OC9001`/`OC9002` names reserve an unexpected
parser/checker extension failure boundary; they do not advertise or provide a security sandbox.
Any downstream Python integration added to the OpenCollate process has that process’s permissions
and must be trusted and isolated independently.

## No-signoff guarantee

Security-conscious parsing does not make OpenCollate a verification or signoff engine. A clean
run does not prove that unsupported syntax was irrelevant, a dynamic SDC/UPF script would behave
the same in a native tool, a circuit is electrically correct, or a layout is manufacturable.
Review completeness diagnostics and use native verification/signoff tools for those claims.

Report suspected vulnerabilities through [SECURITY.md](../SECURITY.md), not a public issue.
