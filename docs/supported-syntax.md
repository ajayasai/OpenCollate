# Supported syntax

This document describes the 0.1 Alpha boundary. "Tolerated" means OpenCollate can skip a construct
without losing synchronization; it does not mean the construct participates in checks.

Run `opencollate capabilities` for the installed build.

## Verilog and SystemVerilog

The RTL adapter uses pyslang for preprocessing, parsing, and elaboration.

Supported:

- Verilog and SystemVerilog module definitions.
- ANSI and non-ANSI port declarations.
- Input, output, and inout directions.
- Constant-evaluable packed and unpacked dimensions.
- Parameters needed to evaluate port shapes.
- Include directories, preprocessor defines, and explicit top selection.
- Escaped identifiers and source spans.

Explicitly unsupported or inconclusive:

- Interface and modport ports in the canonical port model.
- `ref` ports.
- Shapes whose dimensions cannot be evaluated in the available elaboration context.
- Arbitrary behavioral equivalence or sequential logic.

RTL Boolean extraction is limited to simple scalar-output continuous `assign` statements;
procedural blocks are not interpreted as Boolean functions in 0.1.

Parser or elaboration errors can taint dependent observations. OpenCollate does not fall back to a
regular-expression parser or assume a failed declaration is scalar.

## Liberty

Supported structurally:

- `library`, `cell`, `pin`, `bus`, `bundle`, `type`, and `pg_pin` groups.
- Pin direction and `use`.
- Bus type and range metadata.
- Boolean `function` attributes.
- Quoted strings, comments, nested groups, and common vendor attributes.

Liberty group nesting is limited to 128 levels. Deeper input is rejected as a fatal parse error
instead of relying on the Python recursion limit.

Tolerated but not interpreted:

- Timing arcs and lookup-table values.
- Internal power, leakage, noise, and characterization tables.
- Wire-load and operating-condition models.

The Boolean grammar covers constants, identifiers, parentheses, prefix `!` and `~`, postfix
complement, AND, XOR, and OR spellings commonly used by Liberty. Implicit adjacency is AND.
Equivalence is bounded by `policy.max_boolean_inputs`. To keep adversarial collateral bounded,
one expression is limited to 65,536 characters, 4,096 tokens, and 128 nested groups; exceeding a
limit is reported as unsupported rather than crashing the check.

## LEF

Supported:

- `VERSION`, `BUSBITCHARS`, and `DIVIDERCHAR`.
- `MACRO` and nested `PIN` blocks.
- Pin `DIRECTION` and `USE`.
- Bus names using the declared bus-bit characters.

Tolerated but not interpreted:

- Port geometry, layers, vias, rectangles, polygons, obstructions, and symmetry.
- Antenna properties and vendor extensions.

Geometry is ignored while `MACRO`/`PIN`/`END` scope tracking prevents it from becoming interface
facts.

## CSV

Supported:

- RFC 4180 quoting and embedded delimiters.
- UTF-8 with or without BOM.
- Configurable header aliases.
- Scalar, ranged, and per-bit rows.
- Direction and signal-role normalization.
- `component_pins` and `package_map` profiles.

The `package_map` profile can map die pad, package ball, signal, and component columns. Columns
that affect identity must be mapped explicitly when built-in aliases do not match. Duplicate or
gapped bit rows are retained and diagnosed.

## Not supported in 0.1

IP-XACT, SDC, UPF, DEF, CDL, GDSII, software register headers, and arbitrary documentation are
roadmap items, not silently ignored inputs.
