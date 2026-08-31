# Changelog

All notable changes to OpenCollate will be documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

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

[Unreleased]: https://github.com/ajayasai/OpenCollate/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ajayasai/OpenCollate/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ajayasai/OpenCollate/releases/tag/v0.1.0
