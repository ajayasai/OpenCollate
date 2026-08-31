module top (
    input  logic a,
    output logic y,
    output logic selected
);
    logic middle;

    assign middle = a;
    assign y = middle;
    assign selected = a & y;
endmodule
