# Configuration

OpenCollate reads TOML. The default file is `opencollate.toml`; pass another path positionally or
with `-c`/`--config`.

```console
opencollate init .
```

Paths are resolved relative to the configuration file after applying `project.root`. A source
accepts `files = [...]` or the singular `path = "..."`; glob expansion is deterministic and an
empty configured glob is fatal.

## Full-stack project

This is the shape used by the runnable [UART example](../examples/uart/opencollate.toml):

```toml
schema_version = 1

[project]
name = "uart-demo"
root = "."

[sources.rtl.default]
files = ["rtl/uart.sv"]
include_dirs = []
defines = []
top = "uart"

[sources.liberty.tt]
files = ["lib/uart.lib"]

[sources.lef.abstract]
files = ["lef/uart.lef"]

[sources.csv.package]
files = ["package/pins.csv"]
profile = "package_map"

[sources.csv.package.columns]
die_pad = "Pad"
package_ball = "Ball"
signal = "Signal"
component = "Component"

[sources.ipxact.component]
files = ["ipxact/uart.xml"]

[sources.sdc.functional]
files = ["constraints/uart.sdc"]

[sources.upf.power]
files = ["power/uart.upf"]

[sources.header.software]
files = ["software/uart_regs.h"]
component_name = "uart"
macro_prefix = "UART"
default_register_width = 32

[sources.cdl.netlist]
files = ["netlist/uart.cdl"]

[sources.def.placed]
files = ["physical/uart.def"]

[sources.gds.stream]
files = ["physical/uart.gds"]
top_cells = ["uart"]
# No label is a port unless at least one selector is explicit.
pin_text_layers = [10]
pin_text_types = [5]

[contract]
baseline = "rtl.default"

[policy]
strict_inventory = false
rtl_power_pins = "optional"
scalar_vector_equivalent = false
max_boolean_inputs = 12
```

## View IDs and registered kinds

Tables use `[sources.<kind>.<name>]`; the full view ID is `kind.name`. Names preserve distinct
corners or variants, such as `liberty.tt` and `liberty.ss`.

The canonical configuration kinds are `rtl`/`verilog`, `liberty`, `lef`, `csv`, `ipxact`, `sdc`,
`upf`, `header`, `cdl`, `def`, and `gds`. Common dispatch aliases and file extensions are accepted
by the Python parser registry, but explicit configuration kind names are preferable for
reviewability. GDS aliases include `gdsii`, `gds2`, and `stream`; `.gds` and `.gdsii` extensions
are inferred as GDSII.

## Source-specific options

Unknown options are fatal rather than ignored.

| Source kind | Accepted options |
| --- | --- |
| RTL/Verilog/SystemVerilog | `include_dirs`, `defines`, and `top` as a nonempty string or string array |
| Liberty | No parser-specific options |
| LEF | No parser-specific options |
| CSV | `profile`, `columns`, `component_name`, and one-character `delimiter` |
| IP-XACT | `parameter_values`, a string-to-integer map for otherwise external integer parameters |
| SDC | No parser-specific options; multiple files share ordered static scalar variables |
| UPF | Optional nonempty `component_name` when the source does not establish the design top |
| C header | Optional nonempty `component_name`, nonempty `macro_prefix`, and positive integer `default_register_width` |
| CDL/SPICE | No parser-specific options |
| DEF | No parser-specific options |
| GDSII | Optional `top_cells` string/string array plus integer or integer-array `pin_text_layers` and `pin_text_types` selectors in the range 0–32767 |

`profile` and `columns` are top-level source keys. The remaining parser-specific values are
forwarded only after type and allow-list validation.

`opencollate init` includes a commented GDSII source block with all three options. With no
`top_cells`, the parser selects unreferenced structures as inferred tops; an explicit absent top
is diagnosed rather than invented. With neither text selector, labels remain text observations
and the GDSII view defines no ports. With one selector, it must match; with both, a label must
match both. Selected ports intentionally retain unknown direction, role, and logical shape.

## Contract selection

`contract.baseline` identifies a preferred source view where a rule or generated contract needs
intent. A frozen reviewed contract can be loaded instead:

```toml
[contract]
file = "contract.oc.json"
```

Authority can be selected per contract category:

```toml
[contract.authority]
components = "rtl.default"
ports = "rtl.default"
registers = "ipxact.component"
```

Authority chooses among known candidates; it does not suppress conflicting source evidence.

## Aliases, participation, and waivers

These are separate mechanisms:

- **Aliases** state that differently named source objects represent one canonical identity.
- **Participation** states which views are expected to contain a component or role.
- **Waivers** accept a narrowly identified diagnostic for a documented reason and optional expiry.

Do not use an alias to conceal absence, participation to conceal a conflicting value, or a waiver
to conceal parser incompleteness. Fatal findings cannot be waived or downgraded. Match waivers on
code plus stable object/property/fingerprint where possible; broad code-only waivers are fragile.

## Policy

Core policy fields are:

| Key | Meaning |
| --- | --- |
| `strict_inventory` | Require configured component views to contain the reconciled inventory |
| `rtl_power_pins` | Treat RTL power pins as `optional`, `required`, or `ignore` |
| `scalar_vector_equivalent` | Allow an explicit scalar and one-bit vector to compare as equivalent |
| `max_boolean_inputs` | Exact Boolean truth-table input bound, from 1 to 24 |
| `compare_functions` | Enable supported RTL/Liberty Boolean checks |
| `deny_warnings` | Make unwaived warnings fail the policy result |
| `report_unmatched_waivers` | Emit informational diagnostics for stale selectors |
| `allow_multi_bond` | Permit explicitly intended multiple package bonds where supported |

Severity overrides are policy, not evidence transformation. They cannot turn unknown or tainted
facts into known facts. Keep configuration and frozen contracts in version control and review
them like source code.
