# Changelog

All notable changes to OpenCollate will be documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

## [0.3.0] - 2026-08-31

### Added

- Ordered SystemRDL 2.0 import through `systemrdl-compiler`, with preflight rejection of Perl
  preprocessing and source includes, selected-top support, nested address maps and register files,
  arrays, absolute addresses, register widths, fields, access, resets, and provenance.
- Declarative connectivity CSV import and bounded static RTL graph checks for required and
  forbidden transparent paths, identity/reversed bit ordering, known inversion, waypoints,
  exclusions, witnesses, cuts, and inconclusive tainted frontiers.
- Git-native `review` and `report diff` commands that classify findings as new, changed,
  unchanged, or resolved using stable fingerprints and deterministic content digests. A strict
  Draft 2020-12 diff schema and configurable CI failure policies are included.
- A schema-validated public benchmark harness with exact conformance oracles, deterministic
  result digests, per-case time budgets, and nightly runs for report diffing, the full UART,
  SystemRDL import, and RTL connectivity.
- SystemRDL/register and connectivity rule families, bringing the runtime catalog to 74 rules.
- A thirteen-view synthetic UART example covering every supported input kind.
- Optimized-Python invariant testing and adversarial/property coverage for the new analysis
  surfaces.

### Changed

- CSV pin maps now have enforced `auto`, `component_pins`, and `package_map` profiles; invalid or
  inapplicable source options fail explicitly instead of being silently ignored.
- Configured source patterns now preserve declaration order, sort only within each glob, and
  deduplicate by first occurrence so ordered compilation units remain ordered.
- Nested SystemRDL and IP-XACT registers now share address-block-relative offsets and explicit
  register-file hierarchy, including repeated register names in sibling register files.
- Static RTL connectivity now models net aliases plus transparent buffer/inverter/transmission
  primitives, accepts generated hierarchy indices, and collision-proofs reserved characters in
  escaped identifiers with documented percent encoding.
- Release SBOM verification now derives the expected package inventory from every declared
  runtime dependency.

### Security

- SystemRDL input is preflighted before the compiler backend, compiled from the captured private
  snapshot, and never allowed to execute Perl tags, follow source includes, or generate artifacts.
- UPF parsing now has shared multi-file caps for source size, commands, tokens, static list
  expansion, nesting, names, and emitted observations; limit hits stop with fatal global taint.
- Connectivity parsing and graph exploration publish bounded input, selector, edge, and traversal
  limits. Exact-transform results fail closed when unsupported or tainted RTL could affect the
  selected endpoints.
- SystemRDL, connectivity CSV, and saved-report reads enforce their byte limits at the stream
  boundary. Saved reports also require UTF-8, bounded JSON nesting, and strict schema structure.
- Required CI runs the complete suite with Python assertions disabled, in addition to branch
  coverage, static typing, linting, and security linting.

### Known limitations

- Connectivity checks cover a documented transparent static RTL subset. They are not temporal,
  sequential, conditional, mode-aware, or formal proofs.
- SystemRDL support imports selected structural register facts; it does not implement code
  generation or verify register behavior.
- OpenCollate remains a collateral-consistency checker, not a timing, power-intent, physical,
  functional, or tapeout signoff tool.

## [0.2.1] - 2026-08-31

### Fixed

- Release SPDX SBOM generation now inventories the built OpenCollate package and its runtime
  dependency closure instead of only the distribution directory.
- Release validation now fails before publication unless the dependency-inclusive SBOM contains
  OpenCollate and its declared runtime dependencies.

## [0.2.0] - 2026-08-31

### Added

- Secure, namespace-tolerant IP-XACT 2009, 2014, and 2022 component import, including ports,
  interfaces, parameters, memory maps, registers, and fields.
- Static, non-executing SDC and UPF importers. Tcl-shaped collateral is tokenized and bounded
  facts are extracted without starting Tcl or running source commands.
- Conventional C register-header import with bounded, side-effect-free integer expressions.
- Structural CDL/SPICE import for subcircuits, explicit pin metadata, connectivity, globals,
  models, and common M/R/C/L/X device forms without simulation or parameter evaluation.
- Structural DEF 5.8 import for design interfaces, components, placements, nets, special nets,
  and RTL endpoint validation while route geometry is safely skipped.
- Experimental native GDSII import with bounded big-endian record parsing, cell structures,
  SREF/AREF hierarchy, transforms, top-cell selection, and text labels. Geometry records are
  skipped without polygon materialization; labels become candidate ports only through explicit
  layer and/or text-type selectors.
- First-class design-object references, clocks, IP-XACT interfaces, and register observations
  with source provenance and explicit fact state.
- SDC/RTL, UPF/RTL, IP-XACT interface, IP-XACT/C-header register, and DEF/RTL rule families,
  expanding the runtime catalog to 65 rules.
- A runnable eleven-view synthetic UART example covering every 0.2.0 input format.
- Exact syntax, security, unknown-state, contract, configuration, and no-signoff documentation.

### Security

- IP-XACT rejects DTD and entity declarations and never fetches schemas or external definitions.
- SDC and UPF do not execute Tcl; C-header and IP-XACT expressions use bounded static evaluators.
- CDL/SPICE is never simulated, DEF/LEF geometry is not interpreted, and parser limits fail
  closed with explicit diagnostics.
- GDSII validates bounded record/container structure and discards geometry coordinates after
  structural validation instead of constructing polygons.
- Production parser invariants fail closed under optimized Python, and Bandit security lint is
  enforced in local hooks and required CI.

### Known limitations

- GDSII support is experimental and structural only; it performs no geometry verification, DRC,
  LVS, extraction, or implicit connectivity inference.
- Static SDC/UPF coverage is intentionally smaller than full Tcl-based tool behavior.
- OpenCollate does not perform signoff analysis and a clean report is not tapeout certification.

## [0.1.0] - 2026-08-31

### Added

- Initial Alpha release of the canonical SoC design-contract model.
- Verilog/SystemVerilog, Liberty, LEF, and CSV pin-map importers.
- Cross-view inventory, port, bus, power/ground, and Boolean-function checks.
- Human-readable diagnostics plus JSON, SARIF, and Markdown reporting surfaces.
- Synthetic UART example and public parser/check conformance documentation.

[Unreleased]: https://github.com/ajayasai/OpenCollate/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ajayasai/OpenCollate/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/ajayasai/OpenCollate/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ajayasai/OpenCollate/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ajayasai/OpenCollate/releases/tag/v0.1.0
