`ifndef OPENCOLLATE_OPS_SVH
`define OPENCOLLATE_OPS_SVH
`ifdef INVERT_DATA
`define TRANSFER(data) (~(data))
`else
`define TRANSFER(data) (data)
`endif
`endif
