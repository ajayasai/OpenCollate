module lane #(parameter W=8)(input logic clk, rst, input logic [1:0] op,
    input logic [W-1:0] d, output logic [W-1:0] q);
    logic [W-1:0] n;
    always_comb begin
        n = q;
        case (op)
            0: n = d;
            1: n = q + 1'b1;
            2,3: n = q ^ d;
            default: n = 0;
        endcase
    end
    always_ff @(posedge clk) begin
        if (rst) q <= 0;
        else q <= n;
    end
endmodule
module top(input logic clk, rst, input logic [1:0] op,
    input logic [7:0] d, output wire [7:0] q);
    lane #(.W(8)) unit(.clk(clk), .rst(rst), .op(op), .d(d), .q(q));
endmodule
