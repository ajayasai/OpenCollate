"""Synthetic, redistributable OpenCollate demonstration project."""

from __future__ import annotations

from pathlib import Path

DEMO_FILES: dict[str, str] = {
    "opencollate.toml": """\
schema_version = 1

[project]
name = "synthetic-uart-demo"

[sources.rtl.default]
files = ["rtl/uart.sv"]
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

[contract]
baseline = "rtl.default"

[policy]
rtl_power_pins = "optional"
max_boolean_inputs = 12
""",
    "rtl/uart.sv": """\
// Synthetic demonstration; not production UART RTL.
module uart (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic tx_en_i,
    output logic irq_o,
    output logic tx_active_o,
    inout  wire  VDD_CORE,
    inout  wire  VSS
);
    assign irq_o = tx_en_i & rst_ni;
    assign tx_active_o = tx_en_i & ~rst_ni;
endmodule
""",
    "lib/uart.lib": """\
/* Synthetic demonstration with deliberate inconsistencies. */
library (synthetic_uart_lib) {
  cell (uart) {
    pin (clk_i) { direction : input; clock : true; }
    pin (rst_ni) { direction : input; }
    pin (tx_en_i) { direction : input; }
    pin (irq_o) {
      direction : input;
      function : "tx_en_i & rst_ni";
    }
    pin (tx_active_o) {
      direction : output;
      function : "tx_en_i & rst_ni";
    }
    pg_pin (VDD_CORE) { pg_type : primary_power; }
    pg_pin (VSS) { pg_type : primary_ground; }
  }
}
""",
    "lef/uart.lef": """\
VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
MACRO uart
  PIN clk_i
    DIRECTION INPUT ;
    USE CLOCK ;
  END clk_i
  PIN rst_ni
    DIRECTION INPUT ;
    USE SIGNAL ;
  END rst_ni
  PIN tx_en_i
    DIRECTION INPUT ;
    USE SIGNAL ;
  END tx_en_i
  PIN irq_o
    DIRECTION OUTPUT ;
    USE SIGNAL ;
  END irq_o
  PIN tx_active_o
    DIRECTION OUTPUT ;
    USE SIGNAL ;
  END tx_active_o
  PIN VDD_CORE
    DIRECTION INOUT ;
    USE POWER ;
  END VDD_CORE
  PIN VSS
    DIRECTION INOUT ;
    USE GROUND ;
  END VSS
END uart
END LIBRARY
""",
    "package/pins.csv": """\
Pad,Ball,Signal,Component
PAD_CLK,A1,clk_i,uart
PAD_IRQ,B1,irq_o,uart
PAD_TX_ACTIVE,B1,tx_active_o,uart
PAD_VDD,C1,VDD_CORE,uart
PAD_VSS,C2,VSS,uart
""",
}


def write_demo(directory: str | Path) -> Path:
    """Materialize the synthetic demo without overwriting existing files."""

    root = Path(directory).expanduser().resolve()
    collisions = [root / name for name in DEMO_FILES if (root / name).exists()]
    if collisions:
        names = ", ".join(str(path) for path in collisions[:3])
        raise FileExistsError(f"demo destination already contains OpenCollate files: {names}")
    for relative, content in DEMO_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return root


__all__ = ["DEMO_FILES", "write_demo"]
