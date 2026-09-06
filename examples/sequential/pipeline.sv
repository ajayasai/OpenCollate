// Two-stage synchronous pipeline. Each sample is taken just before a rising edge.
module pipeline # (parameter WIDTH = 8) (
    input logic clk,
    input logic rst_n,
    input logic enable,
    input logic [WIDTH-1:0] data_i,
    output logic [WIDTH-1:0] data_o
);
    logic [WIDTH-1:0] stage0;
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            stage0 <= '0;
            data_o <= '0;
        end else if (enable) begin
            stage0 <= data_i;
            data_o <= stage0;
        end
    end
endmodule
