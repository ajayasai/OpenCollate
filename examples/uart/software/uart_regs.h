/* Synthetic software register contract for the OpenCollate UART example. */
#ifndef OPENCOLLATE_EXAMPLE_UART_REGS_H
#define OPENCOLLATE_EXAMPLE_UART_REGS_H

#define UART_BASE                     0x40001000UL
#define UART_CTRL_OFFSET              0x00UL
#define UART_CTRL_ADDR                (UART_BASE + UART_CTRL_OFFSET)
#define UART_CTRL_ENABLE_POS          0U
#define UART_CTRL_ENABLE_MASK         (0x1UL << UART_CTRL_ENABLE_POS)
#define UART_CTRL_ENABLE_RESET        0U
#define UART_CTRL_MODE_POS            4U
#define UART_CTRL_MODE_MASK           (0x3UL << UART_CTRL_MODE_POS)

#endif
