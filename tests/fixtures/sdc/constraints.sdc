# Static variables, quoting, braces, semicolons, and continuations are intentional.
set CLK_PORT "clk_i"
set PERIOD 10.0
set IO_PORTS {rx_i tx_o}

create_clock -name core_clk -period $PERIOD \
    -waveform {0.0 5.0} [get_ports $CLK_PORT]

set GENERATED_TARGET [get_pins {u_div/clk_q}]
create_generated_clock -name div_clk \
    -source [get_pins {u_div/clk_i}] \
    -master_clock [get_clocks core_clk] \
    -divide_by 2 $GENERATED_TARGET

set_input_delay 1.25 -clock [get_clocks core_clk] [get_ports rx_i]
set_output_delay 2.5 -clock core_clk [get_ports tx_o]
set_false_path -from [get_cells {u_async_src}] \
    -through [get_pins {u_sync/ff1/D}] \
    -to [get_pins {u_sync/ff2/D}]
set_multicycle_path 2 -setup \
    -from [get_clocks core_clk] -to [get_clocks div_clk]

get_ports {rst_n data[*]}; # A static glob remains an explicit dynamic query.
