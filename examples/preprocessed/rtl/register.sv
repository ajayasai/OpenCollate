`timescale 1ns/1ps
`default_nettype none
`include "ops.svh"
module transfer_register #(parameter integer WIDTH = `DATA_WIDTH) (
  input logic clk,
  input logic [WIDTH-1:0] d,
  output logic [WIDTH-1:0] q
);
  always_ff @(posedge clk) q <= `TRANSFER(d);
endmodule
`default_nettype wire
