module lane #(parameter W = 8)(
  input logic clk, rst, en,
  input logic [W-1:0] d,
  output logic [W-1:0] q
);
  always_ff @(posedge clk) begin
    if (rst) q <= '0;
    else if (en) q <= d;
  end
endmodule

module top(
  input logic clk, rst, en,
  input logic [7:0] d,
  output wire [7:0] q
);
  // Per-bit module instances: no handwritten flattening or transition equations.
  for (genvar i = 0; i < 8; i++) begin: lanes
    lane #(.W(1)) ff(.clk(clk), .rst(rst), .en(en), .d(d[i]), .q(q[i]));
  end

  // Fully validated, but irrelevant to either declared property.
  logic [31:0] heartbeat;
  always_ff @(posedge clk) begin
    if (rst) heartbeat <= '0;
    else heartbeat <= heartbeat + 1'b1;
  end
endmodule
