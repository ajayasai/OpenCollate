module delay_cell #(parameter W=8, INVERT=0)(input wire clk,rst_n,en,
 input wire [W-1:0] d, output logic [W-1:0] q);
 always_ff @(posedge clk) if (!rst_n) q<=0;
 else if (en) q<=INVERT ? ~d : d;
endmodule
module counter_cell #(parameter W=32)(input wire clk,rst_n, input wire [W-1:0] d,
 output logic [W-1:0] count);
 always_ff @(posedge clk) if(!rst_n) count<=0; else count<=count+d+1'b1;
endmodule
