# Configuration

OpenCollate reads TOML. The default file is `opencollate.toml`; pass another path positionally or
with `-c` or `--config`.

Generate a concise starting point:

```console
opencollate init .
```

## Minimal four-view project

```toml
schema_version = 1

[project]
name = "uart-demo"
root = "."

[sources.rtl.default]
files = ["rtl/uart.sv"]
include_dirs = []
defines = []

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

[contract]
baseline = "rtl.default"

[policy]
strict_inventory = false
rtl_power_pins = "optional"
scalar_vector_equivalent = false
max_boolean_inputs = 12
```

Paths are resolved relative to the configuration file after applying `project.root`. A source
accepts `files` or the singular convenience key `path`.

## Source views

Tables use `[sources.<kind>.<name>]`. The full view ID combines kind and name, for example
`rtl.default` or `liberty.tt`. Names let a project import multiple corners or abstracts without
losing provenance.

Parser-specific options belong inside their source table. Invalid options stop the run; they are
not silently used to establish facts.

## Contract policy

`contract.baseline` selects the view used as authority where a rule needs intent. An optional
frozen contract file can provide a reviewed baseline:

```toml
[contract]
file = "contract.oc.json"
```

## Aliases, participation, and waivers

These are separate concepts:

- **Aliases** state that differently named source objects represent one canonical identity.
- **Participation** states which views are expected to contain an object.
- **Waivers** accept one diagnostic for a documented reason.

Do not use an alias to conceal absence, a participation rule to conceal a conflicting value, or a
waiver to conceal parser incompleteness. The file produced by `opencollate init` intentionally
omits these project-specific tables; add them after deciding the identities, expected views, and
narrowly justified exceptions for the design.

## Policy

Policy chooses intentional project behavior, including strict inventory, RTL power-pin
participation, scalar/vector equivalence, and the maximum Boolean truth-table input count. Keep
policy in version control and review it like source code.
