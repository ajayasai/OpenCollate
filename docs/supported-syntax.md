# Supported syntax

This document defines the OpenCollate 0.3.0 Beta parsing boundary. “Supported” means the
construct produces typed observations used by one or more rules. “Tolerated” means a parser can
skip the construct without losing structural synchronization. Neither word implies complete
language implementation or native-tool equivalence.

Run `opencollate capabilities` for the installed build. When this document and an installed
binary differ, the binary’s capability output and diagnostics describe the code actually running.

## Completeness and unknowns

Each observed fact has one state:

- `known`: the parser established the value inside its documented subset.
- `unknown`: the source or available context did not establish a value.
- `unsupported`: the construct was recognized but is outside the modeled subset.
- `tainted`: parser recovery or unresolved input may have affected the fact.
- `not_applicable`: the property has no meaning for that object or view.

Only known facts establish equality. Unknown, unsupported, and tainted facts are retained and can
produce `OC1102`–`OC1105`; they never become a guessed scalar, name, direction, address, or pass.

## Verilog and SystemVerilog

The RTL adapter uses pyslang for preprocessing, parsing, and elaboration.

Supported:

- Verilog and SystemVerilog module definitions and statically elaborated hierarchy.
- ANSI and non-ANSI port declarations; input, output, and inout directions.
- Constant-evaluable packed and unpacked dimensions and parameters required by port shapes.
- Include directories, preprocessor defines, and one or more explicit top names.
- Escaped identifiers and source spans.
- Simple scalar-output continuous assignments for bounded Boolean comparison with Liberty.
- A bit-level static connectivity graph for transparent continuous assignments, exact net
  aliases, `buf`/`not` and `tran`/`rtran` primitives, hierarchy port bindings, constant
  ranges/element selects, concatenation, order reversal, and bitwise inversion.

Unsupported or inconclusive:

- Interface and modport ports in the canonical port model, and `ref` ports.
- Shapes whose dimensions cannot be evaluated in the selected elaboration context.
- Arbitrary procedural, sequential, temporal, or behavioral equivalence.
- Simulation semantics, assertions, timing checks, synthesis, and formal proof.

Procedural logic, dynamic selections, arithmetic/data transforms, width-changing conversions, and
other non-transparent cones form a tainted connectivity frontier. They can make a connectivity
requirement inconclusive but can never prove a required or forbidden path. Static connectivity is
bounded to 250,000 endpoints, 1,000,000 edges, 4,096 bits per flattened vector, and a 16,384-edge
cross product at one unsupported frontier.

Parser or elaboration errors taint dependent observations. OpenCollate does not fall back to a
regular-expression parser.

## Liberty

Supported structurally:

- `library`, `cell`, `pin`, `bus`, `bundle`, `type`, and `pg_pin` groups.
- Pin direction and `use`, power/ground roles, bus types, and range metadata.
- Boolean `function` attributes.
- Quoted strings, comments, nested groups, and common vendor attributes needed to stay synchronized.

Tolerated but not interpreted:

- Timing arcs and lookup-table values.
- Internal power, leakage, noise, characterization, wire-load, and operating-condition data.

Group nesting is capped at 128. The Boolean grammar covers constants, identifiers, parentheses,
prefix `!`/`~`, postfix complement, common AND/XOR/OR spellings, and implicit adjacency-as-AND.
Equivalence is bounded by `policy.max_boolean_inputs` (12 by default, configurable from 1 to 24).
One expression is limited to 65,536 characters, 4,096 tokens, and 128 nested groups.

## LEF

Supported:

- `VERSION`, `BUSBITCHARS`, and `DIVIDERCHAR`.
- `MACRO` with nested `PIN` blocks.
- Pin `DIRECTION`, `USE`, and bus names using the declared bus-bit characters.

Tolerated but not interpreted:

- Port geometry, layers, vias, rectangles, polygons, obstructions, symmetry, antenna properties,
  and vendor extensions.

Geometry is skipped while macro/pin/end scope tracking prevents geometry tokens from becoming
interface facts. OpenCollate performs no LEF design-rule or manufacturability analysis.

## CSV component and package maps

Supported:

- RFC 4180 quoting, embedded delimiters, UTF-8 with or without BOM, CSV and one-character custom
  delimiters such as tab.
- `component_pins` and `package_map` profiles.
- Configurable column names, scalar/ranged/per-bit rows, direction, and role normalization.
- Component, signal, die-pad, and package-ball relationships.

Identity-bearing vendor columns must be mapped explicitly when built-in aliases do not match.
Duplicate, conflicting, incomplete, or gapped rows remain observable and are diagnosed. A CSV
package map is a partial mapping source, not a complete component interface.

## IP-XACT

The XML adapter accepts the common IEEE 1685 namespace URIs for 2009, 2014, and 2022 component
documents.

Supported:

- Component VLNV metadata and component ports.
- Wire directions, packed vectors, arrays, and clock/reset/power qualifiers where present.
- Integer parameters and bounded integer expressions, including configured `parameter_values`.
- Bus interfaces, modes, bus/abstraction references, and logical-to-physical port maps.
- Memory maps, address blocks, nested register files, registers, fields, access, and reset values.
- Address-unit, base-address, offset, size, range, and source provenance needed for register checks.

Security and limits:

- No XSD validation, schema download, XInclude, DTD, or entity expansion. DTD/entity declarations
  are rejected.
- External `memoryMapDefinitionRef`, `addressBlockDefinitionRef`, and `registerDefinitionRef`
  targets are not fetched or expanded; affected facts are unsupported.
- XML input is capped at 64 MiB of decoded characters, 1,000,000 elements, and 256 levels.
- Integer evaluation is capped at 256 expression nodes, 64 levels, and 4,096-bit results.

Unsupported namespaces are retained as tainted local evidence rather than silently treated as a
known IEEE version.

## SystemRDL 2.0

SystemRDL is compiled with the public `systemrdl-compiler` 1.32.x node API and converted into the
same register observations used for IP-XACT and C-header comparison.

Supported:

- Explicitly ordered compilation units, selected or default top-level address map, and a configured
  canonical component name.
- Nested address maps and register files, concrete register instances, unrolled register arrays,
  address-map-relative canonical offsets, immediate parent-local offset metadata, and absolute
  addresses.
- Register widths plus field positions, widths, software access, and reset values.
- Definition/instance provenance and deterministic concrete hierarchy paths.

Security and completeness boundaries:

- Perl preprocessing tags and source `` `include `` directives are rejected during a complete
  preflight before the compiler backend is loaded. The backend compiles private snapshots of the
  captured text, not paths that can be swapped after preflight. List every unit explicitly.
- Memories and signals are not imported as registers. User-defined properties and behavioral
  properties outside layout/access/reset comparison remain explicit unsupported metadata.
- Aliased, virtual, overlapping, or otherwise non-comparable layouts are tainted rather than
  flattened into an apparent match.
- Limits are 256 source files, 16 MiB per file, 64 MiB aggregate source, 250,000 node definitions,
  100,000 register definitions, 65,536 elements per array, 250,000 concrete registers, 65,536
  fields per register, 1,000,000 fields total, 128 hierarchy levels, and 256 detailed unsupported
  semantic diagnostics before a bounded summary.

OpenCollate does not generate register RTL, UVM models, software, or documentation and does not
verify register behavior against RTL transactions.

## Declarative static connectivity

Connectivity intent is a strict CSV input under an explicit `connectivity` source kind. Ordinary
`.csv` inference remains the pin/package-map format; use `.occonn` only when extension inference
is desired.

Required columns are `id`, `source`, `sink`, and `expect`. `expect` is `reachable` or
`unreachable`. Optional columns are `transform` (`any`, `identity`, `reverse`, or `inverted`),
semicolon-separated `through` and `exclude` selectors, and `description`.

Selectors name slash-separated elaborated RTL signals and can use a bounded `*`/`?` glob plus an
optional exact bit, `[*]`, or constant range. Elaborated generate and instance-array indices stay
inside path segments, for example `top/lanes[0]/u/data`. To keep names collision-free, reserved
characters inside a SystemVerilog escaped identifier use uppercase percent encoding: `%25`,
`%2F`, `%5B`, `%5D`, `%2A`, `%3F`, `%3B`, and `%5C` represent `%`, `/`, `[`, `]`, `*`, `?`, `;`,
and `\` respectively. Thus escaped net `\a/b` is selected as `top/a%2Fb`, while `top/a/b` names
ordinary hierarchy. A selector must resolve to one signal base; an ambiguous glob is an error. The
checker can establish:

- Required or forbidden transparent static paths, including a witness path or reachable cut.
- Equal endpoint width and declared bit identity/reversal.
- Known inversion parity for scalar inverted paths.
- Ordered waypoint and excluded-node constraints.

This is graph reachability over the documented transparent RTL subset, not formal connectivity.
It has no clocks, latency, temporal sequences, mode constraints, symbolic data values, or proof
engine. A tainted/unsupported edge, incomplete graph frontier, or search bound produces `OC6505`
instead of a false pass.

CSV limits are 32 MiB per file, 250,000 rows, 32 columns, 65,536 characters per field, 4,096
characters per selector, and 64 `through` or `exclude` selectors. Each requirement is capped at
1,024 selected bits and 65,536 endpoint-pair searches; one graph search is capped at 500,000
states. Duplicate IDs and duplicate or contradictory endpoint requirements are tainted and
diagnosed.

## Static SDC

SDC is parsed by a non-executing Tcl tokenizer. OpenCollate does not start Tcl, source another
file, inspect environment variables, or execute command substitution outside its static subset.

Supported:

- Tcl comments, semicolon/newline command boundaries, quoted/braced/bare words, continuations,
  scalar variables, static `set`, `unset`, and `list` forms.
- `get_ports`, `get_pins`, `get_cells`, and `get_clocks` with retained pattern/options metadata.
- `create_clock` and `create_generated_clock`, including statically known targets, source, master,
  divide/multiply/edges/waveform options retained by the model.
- `set_input_delay`, `set_output_delay`, `set_false_path`, and `set_multicycle_path` for statically
  resolvable collections and values.
- Ordered multi-file views; static scalar variables carry across parser input order (configuration
  path expansion itself is deterministic).

Unsupported Tcl commands, control flow, procedures, external commands, array variables, dynamic
command names, and ambiguous substitutions are reported and not executed. Complex regular
expressions that cannot be safely matched are inconclusive, not absent-object errors.

Limits are 4 MiB decoded characters per source, 100,000 commands, 250,000 words, and 128 levels
of grouping/evaluation nesting.

## Static UPF

UPF is also tokenized without running Tcl. Supported structural commands are:

- `upf_version`, `set_design_top`, and `set_scope`.
- `create_power_domain`, `create_supply_port`, `create_supply_net`, `create_supply_set`,
  `connect_supply_net`, and `set_domain_supply_net`.
- `set_isolation`, `set_isolation_control`, `set_retention`, `set_retention_control`, and
  `set_level_shifter`.
- `create_power_switch`, `add_port_state`, and `add_power_state`.

The parser retains definitions, references, scopes, options, supply functions, control signals,
and provenance. Tcl substitution, control flow, procedures, sourced scripts, and unrecognized
commands are unsupported and taint affected facts; they are never executed. OpenCollate does not
evaluate power-state logic, resolve voltages electrically, or reproduce a complete IEEE 1801
implementation. The UPF tokenizer publishes explicit aggregate input caps; deployments handling
untrusted collateral should still apply normal process-level memory and time limits.

Limits are 256 source files; 16 MiB and 16 Mi decoded characters per file; 64 MiB and
64 Mi decoded characters per view; 500,000 physical and 250,000 logical commands; 2 million
lexical tokens and 2 million evaluated static-list words; 1 MiB per token or evaluated word;
16,384 characters per object/scope name; 128 levels of grouping, substitution, or static-list
evaluation; and 2 million emitted objects, ports, and components. Budgets are shared across every
file in a view. A breach stops parsing with a fatal diagnostic and global taint.

## C register headers

Supported conventions:

- Object-like `#define` macros ending in common base, address, register-offset, field-position,
  field-mask, field-width, and field-reset suffixes.
- C integer suffixes and a bounded expression subset covering integer literals, named object-like
  macros, casts to common integer types, unary `+`/`-`/`~`, arithmetic, shifts, and bitwise ops.
- `component_name`, `macro_prefix`, and `default_register_width` source options.
- C/C++ line comments, block comments, and backslash continuations.

OpenCollate does not invoke a C preprocessor or compiler, choose conditional branches, expand
function-like macros, follow includes, or execute macro text. Conflicting definitions and
unresolved/cyclic expressions taint affected registers.

Limits are 16 MiB decoded characters, 200,000 macros, 4,096 characters and 512 syntax nodes per
integer expression, 64 macro-expansion levels, and 4,096-bit integer results.

## CDL and structural SPICE

Supported:

- Case-insensitive `.SUBCKT`/`.ENDS`, `.GLOBAL`, `.MODEL`, `.PARAM`/`.PARAMS`, and `.END` structure.
- Continuation lines and common escaped or hierarchical names.
- Explicit pin metadata from `.PININFO`, `.PORT_DIRECTION`, `.PIN`, `.PORT`, and DSPF pin comments.
- Structural M, R, C, L, and X instance forms, nets, subcircuit references, models, and parameters
  preserved as text.

OpenCollate never simulates, evaluates parameters, expands expressions, loads device models, or
claims electrical equivalence. Unsupported devices/directives remain explicit. CDL subcircuit
pins do not declare logical bus width, so their shape is normally unknown even when the terminal
name corresponds to a vector in another view.

Limits are 16 MiB decoded characters, 1 MiB per physical line, 250,000 logical lines, 2,000,000
tokens, 65,536 tokens per logical line, 128 grouping levels, 16,384 characters per name, and
2,000,000 emitted objects.

## DEF 5.8

Supported structurally:

- `VERSION`, `DIVIDERCHAR`, `BUSBITCHARS`, `DESIGN`, `UNITS`, and section counts.
- `COMPONENTS` instance/master definitions and `PLACED`, `FIXED`, `COVER`, or `UNPLACED` status;
  coordinates and orientation are retained.
- `PINS` names, net, direction, use/role, range/per-bit bus grouping, placement, and layer metadata.
- `NETS` and `SPECIALNETS` top-pin and instance-pin endpoints plus net use.
- Hierarchical and escaped names, comments, entries continued to a semicolon, and source spans.

Routes, vias, polygons, rectangles, masks, and other geometry are clause-skipped rather than
interpreted as connectivity. Unsupported sections are counted and skipped with balanced section
tracking. A placement-only DEF without a `PINS` section does not claim an empty top interface.
Standard DEF connectivity does not establish die-pad-to-package-ball mapping, so the parser emits
no package mapping unless a future explicit, documented construct justifies it.

Limits are 256 MiB per file, 5,000,000 tokens, 65,536 characters per token, 2,000,000 entries per
section, 500,000 tokens per entry, and 256 parenthesis levels. Count mismatches, unclosed entries,
and unsupported clauses produce explicit completeness diagnostics.

## Experimental structural GDSII

The GDSII adapter reads the native big-endian record stream directly. Support is public but
experimental in 0.3.0: it is intended for bounded structure, hierarchy, and explicitly selected
text-label inventory, not layout verification.

Supported structurally:

- Library framing and metadata: `HEADER`, `BGNLIB`, `LIBNAME`, `UNITS`, and `ENDLIB`.
- Cell structures from `BGNSTR`, `STRNAME`, and `ENDSTR`. Every uniquely named structure becomes
  a component observation with byte/record provenance.
- `SREF` and `AREF` hierarchy references, including target, origin or array control points,
  rows/columns, reflection, magnification, and angle metadata.
- `TEXT` labels with layer, text type, one XY origin, string value, and provenance.
- Explicit `top_cells`; otherwise unreferenced structures are inferred as tops. Missing selected
  tops, duplicate structures, dangling references, or reference-only cycles remain explicit.
- `.gds` and `.gdsii` files under source kinds `gds`, `gdsii`, `gds2`, or `stream`.

Text-to-port inference is opt-in. With neither `pin_text_layers` nor `pin_text_types`, every label
remains a text observation and no port is created. With one selector, the label must match it;
with both, it must match both. Labels with the same selected string are grouped into one port.
Selected ports have unknown direction, role, and logical shape—GDSII text does not establish
those facts. Text labels are not die-pad/package-ball mappings and do not establish electrical
connectivity.

Geometry boundary:

- `BOUNDARY`, `PATH`, `TEXTNODE`, `NODE`, and `BOX` elements are framing-checked and counted, but
  their polygons/paths are not materialized. Geometry XY values are bounded and discarded.
- Known presentation, property, mask, extension, datatype, width, and other nonstructural records
  are skipped with counts retained in source metadata.
- OpenCollate performs no layer-purpose interpretation beyond explicit text selectors, polygon
  validation, geometry comparison, DRC, LVS, extraction, parasitic analysis, label-to-shape
  association, or connectivity inference.

Hard limits are 512 MiB per source, 65,534 bytes per record, 10,000,000 records, 1,000,000
structures, 10,000,000 elements, 1,000,000 references and 1,000,000 text labels per structure,
1,000,000 XY points per record, 16,384 bytes per name, 65,530 bytes per text value, and 100,000
skipped records per element. Structure/reference names, text values, and configured top cells must
be nonempty 7-bit ASCII; configured top cells must also be unique, and text selectors are
integers from 0 through 32767. Invalid framing, record data types/sizes, nesting, truncation, or
resource-limit breaches fail closed with tainted or incomplete observations.

## Other inputs

OpenCollate does not accept arbitrary documents as design facts. Convert unsupported collateral
to a documented supported view only when the conversion preserves identity, semantics, and
provenance needed by the check.

## No-signoff boundary

None of these importers substitutes for a language reference implementation or native signoff
tool. OpenCollate does not synthesize, simulate, time, extract, prove, route, run DRC/LVS, validate
power sequencing, or certify tapeout readiness. A clean report covers only enabled rules over
known facts in the supplied configuration.
