# Roadmap

This roadmap communicates direction, not a delivery commitment. Priorities may change based on
real designs, contributor capacity, parser licensing, and standards access.

## 0.1 — Four-view Alpha (delivered)

- Canonical evidence-bearing design contract.
- Verilog/SystemVerilog via pyslang.
- Structural Liberty, LEF macro/pin, and CSV pin-map imports.
- Inventory, direction, shape, role, package mapping, and small Boolean checks.
- Text, JSON, Markdown, SARIF, and contract JSON outputs.

## 0.2 — Full-stack Beta (delivered)

- IEEE 1685 IP-XACT 2009/2014/2022 component, interface, parameter, memory-map, register, and field import.
- Static, non-executing SDC object queries, clocks, generated clocks, delays, and path exceptions.
- Static, non-executing UPF design/scope, domains, supplies, strategies, switches, and power-state import.
- Conventional C register-header import with bounded integer expressions.
- Structural CDL/SPICE subcircuits, pin metadata, connectivity, globals, models, and M/R/C/L/X devices.
- Structural DEF 5.8 design interfaces, components, placements, nets, special nets, and endpoint validation.
- Experimental, bounded native GDSII stream import for cell structures, SREF/AREF hierarchy,
  text labels, explicit label-to-port filters, and top-cell selection without materializing
  geometry.
- Cross-view object reference, clock, IP-XACT interface, register-map, and DEF endpoint rule families.
- An eleven-view synthetic UART example and explicit parser security/unknown-state documentation.

## 0.3 — Physical and conformance depth

- Broader public GDSII hierarchy and text-label conformance fixtures plus performance baselines;
  geometry materialization and physical verification remain out of scope.
- Broader DEF, LEF, CDL/SPICE, SDC, UPF, and IP-XACT conformance corpora.
- Hierarchy-aware matching and richer package/bump-map graph validation.
- Differential-pair, polarity, and voltage-domain consistency rules.
- Fuzzing, adversarial corpus growth, and reproducible parser performance baselines.

## 0.4 — Contract maturity

- Versioned schema migrations for frozen contracts and reports.
- Wider register-description inputs and reserved-bit policies.
- More explicit authority and participation policies for clocks, interfaces, registers, and power intent.
- Diagnostic compatibility guarantees informed by independent users.

## Toward 1.0

- Stable, migration-tested public schemas and Python API boundaries.
- A documented diagnostic compatibility policy.
- A broad permissively licensed conformance corpus across supported formats.
- Reproducible performance and resource-limit baselines on representative public designs.
- At least one full release cycle with independent users and contributors.

OpenCollate is expected to remain a collateral-consistency tool, not a simulation, timing,
physical-verification, or tapeout signoff engine. Requests should describe a concrete cross-view
failure and, when possible, include a minimized synthetic example. See
[CONTRIBUTING.md](CONTRIBUTING.md).
