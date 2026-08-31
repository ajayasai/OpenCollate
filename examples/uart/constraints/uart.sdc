# Synthetic, static SDC. OpenCollate tokenizes this file without running Tcl.
set UART_PERIOD 20.0
create_clock -name uart_clk -period $UART_PERIOD \
    -waveform {0.0 10.0} [get_ports clk_i]
set_input_delay 2.0 -clock [get_clocks uart_clk] [get_ports {tx_en_i data_i}]
set_output_delay 2.5 -clock [get_clocks uart_clk] [get_ports {data_o irq_o tx_active_o}]
