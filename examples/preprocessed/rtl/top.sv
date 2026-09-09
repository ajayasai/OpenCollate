`default_nettype none
module top (
  input logic clk,
  input logic [`DATA_WIDTH-1:0] d,
  output logic [`DATA_WIDTH-1:0] q
);
  transfer_register #(.WIDTH(`DATA_WIDTH)) stage (.clk(clk), .d(d), .q(q));
endmodule
`default_nettype wire
