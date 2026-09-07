module top(input logic clk,rst,go, output logic busy);
  typedef enum logic [1:0] {IDLE=0, WORK=1, DONE=2} phase_t;
  phase_t state, next_state;
  always_comb begin
    next_state=IDLE;
    busy=0;
    case(state)
      IDLE: if(go) next_state=WORK;
      WORK: begin busy=1; next_state=DONE; end
      DONE: next_state=IDLE;
      default: next_state=IDLE;
    endcase
  end
  always_ff @(posedge clk) if(rst) state<=IDLE; else state<=next_state;
endmodule
