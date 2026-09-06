module top(input wire clk,rst_n,en, input wire [7:0] d, output wire [7:0] q);
 wire [7:0] middle;
 delay_cell #(.INVERT(1)) first(clk,rst_n,en,d,middle);
 delay_cell second(clk,rst_n,en,middle,q);
 for(genvar i=0;i<4;i++) begin: peripherals
   wire [31:0] result;
   counter_cell c(clk,rst_n,{24'b0,d},result);
 end
endmodule
