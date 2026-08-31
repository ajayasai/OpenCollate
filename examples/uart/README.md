# Full-stack synthetic UART

This directory is a runnable OpenCollate 0.2.0 Beta showcase. Every file was created for this
project; none is derived from a real IP, library, PDK, package, or product. The example is licensed
under the repository’s Apache-2.0 license.

## Views in one check

| View | File | Facts demonstrated |
| --- | --- | --- |
| SystemVerilog | `rtl/uart.sv` | Elaborated module interface and two simple continuous assignments |
| Liberty | `lib/uart.lib` | Cell/pin/bus interface, power roles, and Boolean functions |
| LEF | `lef/uart.lef` | Macro/pin interface while geometry is skipped |
| CSV package map | `package/pins.csv` | Explicit die-pad, ball, signal, and component relationships |
| IP-XACT 2014 | `ipxact/uart.xml` | Component ports, interface port map, and `CTRL` register/fields |
| Static SDC | `constraints/uart.sdc` | Primary clock plus input/output delays against RTL ports |
| Static UPF | `power/uart.upf` | Top domain, supply ports/nets, connections, and primary supplies |
| C header | `software/uart_regs.h` | The software view of the same `CTRL` register and fields |
| CDL | `netlist/uart.cdl` | Subcircuit interface and explicit pin directions/rail roles |
| DEF 5.8 | `physical/uart.def` | Design pins, bus ranges, placements, nets, and special nets |
| GDSII (experimental) | `physical/uart.gds` | Native cell structure, skipped boundary geometry, and layer/type-filtered text-label ports |

The 560-byte GDSII stream is synthetic; `physical/uart.gds.hex` is its auditable hexadecimal
source representation. Its nine labels become candidate ports only because the configuration
explicitly selects layer 10 and text type 5. Their direction, role, and logical shape remain
unknown, and the boundary polygon is not materialized or verified.

SDC and UPF are static examples: OpenCollate tokenizes them without running Tcl. The header is
read without a C preprocessor, the CDL is not simulated, IP-XACT fetches no schema, DEF/LEF
geometry is not analyzed, and GDSII geometry is bounded and discarded without polygon creation.

## Run it

Install the project with its runtime dependency first:

```console
python -m pip install -e .
opencollate check examples/uart/opencollate.toml
```

Or from this directory:

```console
opencollate check
```

The check is expected to return status 1. Status 1 means analysis completed and found deliberate
collateral violations; status 2 means the run was not trustworthy and is not expected.

Machine-readable output and the selected canonical contract are also runnable:

```console
opencollate check examples/uart/opencollate.toml --format json --output uart-report.json
opencollate contract build examples/uart/opencollate.toml --output uart-contract.oc.json
```

## Deliberate findings

The complete 0.2.0 check reports exactly three seeded rule codes:

1. `OC4001`: `irq_o` is an output in RTL, CDL, DEF, IP-XACT, and LEF but an input in Liberty.
2. `OC4301`: `tx_active_o` implements different Boolean functions in RTL and Liberty.
3. `OC5003`: package ball `B1` is assigned to both `irq_o` and `tx_active_o`.

All other interface, object-reference, clock, power-intent, IP-XACT port-map, register-map, DEF
endpoint, and GDSII cell/text-port facts align. In particular, IP-XACT and the C header agree on
the `CTRL` register address, width, `ENABLE`/`MODE` field layout, and known reset value.

To create a clean derivative:

1. Change the Liberty direction of `irq_o` to `output`.
2. Change the Liberty `tx_active_o` function to `"tx_en_i & !rst_ni"`.
3. Give `tx_active_o` a package ball other than `B1`.

This example demonstrates consistency checking, not a functional UART, geometry check, LVS run,
or tapeout signoff. A clean derivative would only mean the enabled OpenCollate rules found no
contradiction in established facts.
