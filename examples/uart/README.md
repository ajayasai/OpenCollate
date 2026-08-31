# Synthetic UART example

This directory contains a small design created for OpenCollate. It is not derived from a real IP,
standard-cell library, foundry kit, package, or product. The files are licensed under the same
Apache-2.0 license as OpenCollate.

The views intentionally contain three underlying inconsistencies:

1. `irq_o` is an output in RTL and LEF but an input in Liberty.
2. `tx_active_o` implements different Boolean functions in RTL and Liberty.
3. Package ball `B1` is assigned to both `irq_o` and `tx_active_o`.

The current rule set reports `OC4001`, `OC4301`, and `OC5003`. Other facts align so the
diagnostics stay focused.

From the repository root:

```console
opencollate check examples/uart/opencollate.toml
```

From this directory:

```console
opencollate check
```

A completed check is expected to return status 1 because the example contains deliberate
violations. Status 2 means the example could not be checked reliably and should not be treated as
an expected mismatch.

To create a clean derivative, change the Liberty direction of `irq_o` to `output`, change its
`tx_active_o` function to `"tx_en_i & !rst_ni"`, and give `tx_active_o` a unique package ball.
