// Synthetic OpenCollate example. Not production UART RTL.
module uart (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       tx_en_i,
    input  logic [7:0] data_i,
    output logic [7:0] data_o,
    output logic       irq_o,
    output logic       tx_active_o,
    inout  wire        VDD_CORE,
    inout  wire        VSS
);
    assign data_o = data_i;
    // irq_o is intentionally left without behavior; this example checks its interface direction.
    assign tx_active_o = tx_en_i & ~rst_ni;
endmodule
