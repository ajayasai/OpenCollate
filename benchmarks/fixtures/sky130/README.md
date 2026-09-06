# Pinned SkyWater cell-interface corpus

These four **unmodified** upstream files are redistributed under Apache-2.0.
Copyright 2020 The SkyWater PDK Authors. The original per-file notices are retained;
the full license is in the repository's root LICENSE file.

Source repository: `google/skywater-pdk-libs-sky130_fd_sc_hd`.
Source revision: `ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1`.
The manifest records original relative paths, bytes, SHA-256 and Git blob identities.

The Verilog files are generic black-box interfaces with implicit supplies. The
LEF files describe the drive-strength-1 physical variants. The runner explicitly
aliases their component names and permits implicit RTL power pins; no fixture is
rewritten to make it pass. The test proves only structural interface conformance
under those stated policies. It does not verify the cell's behavior or LEF geometry.

Derived mutations are made in temporary copies only: output direction, input
width, and removed physical signal pin. There is no network access at test time.
The source authors do not endorse or certify OpenCollate or these results.
