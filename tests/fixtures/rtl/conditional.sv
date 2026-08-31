`include "defs.svh"

module conditional (
  input logic [`BUS_WIDTH-1:0] payload,
`ifdef ENABLE_IRQ
  output logic irq,
`endif
  output logic \foo.bar
);
`ifdef ENABLE_IRQ
  assign irq = payload[0];
`endif
endmodule
