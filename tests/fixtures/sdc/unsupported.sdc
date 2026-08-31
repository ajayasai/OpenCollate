set UNKNOWN_PERIOD [expr {$BASE_PERIOD / 2}]
create_clock -name uncertain -period $UNKNOWN_PERIOD [get_ports clk_i]
set_false_path -from [all_registers]
foreach port {rx_i tx_o} {
    set_input_delay 1.0 [get_ports $port]
}
