# Roadmap

This roadmap communicates direction, not a delivery commitment. Priorities may change based on
real designs, contributor capacity, parser licensing, and standards access.

## 0.1 — Four-view Alpha

- Canonical evidence-bearing design contract.
- Verilog/SystemVerilog via pyslang.
- Structural Liberty, LEF macro/pin, and CSV pin-map imports.
- Inventory, name, direction, width/range, ordering, power/ground, role, and small Boolean checks.
- Terminal, JSON, and SARIF-oriented reports.
- Stable diagnostic codes, explicit unknown states, aliases, participation rules, and waivers.

## 0.2 — Integration collateral

- IP-XACT component, port, bus-interface, and address-block import.
- SDC object-reference validation against the canonical RTL inventory.
- Richer alias rules and hierarchy-aware object resolution.
- Larger public conformance and performance corpus.

## 0.3 — Power and packaging

- UPF object-reference and power-domain consistency checks.
- Package-ball, die-pad, and bump-map graph validation beyond flat CSV.
- Voltage-domain-aware supply-set checks.
- Differential-pair and polarity consistency.

## 0.4 — Hardware/software contract

- IP-XACT and structured register-map normalization.
- C/C++ register-header versus hardware-address-map checks.
- Reset value, field mask, access policy, and reserved-bit consistency.

## Toward 1.0

- Versioned and migration-tested contract/report schemas.
- A documented diagnostic compatibility policy.
- A broad permissively licensed conformance corpus.
- Parser fuzzing and resource-limit hardening.
- Reproducible performance baselines.
- At least one full release cycle with independent users and contributors.

Requests should describe a concrete cross-view failure and, when possible, include a minimized
synthetic example. See [CONTRIBUTING.md](CONTRIBUTING.md).
