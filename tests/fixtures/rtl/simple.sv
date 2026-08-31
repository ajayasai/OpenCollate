module alu #(
  parameter int WIDTH = 8
) (
  input  logic                 clk,
  input  logic [WIDTH-1:0]     a,
  input  logic [WIDTH-1:0]     b,
  output logic                 y
);
  assign y = a[0] & b[0];
endmodule

module legacy(a, status);
  input [0:3] a;
  output status;
endmodule
