# Changelog

All notable changes to OpenCollate will be documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

### Added

- Canonical contract schema version 2, with deterministic per-view snapshots covering components,
  package mappings, hierarchical objects, clocks, interfaces, registers, static connectivity, view
  attributes, completeness, and tainted scopes. Each snapshot is protected by a verified SHA-256
  content digest and contracts have a bounded, JSON-safe extension namespace.
- `opencollate contract migrate` upgrades schema-version-1 contracts without inventing observation
  families that legacy files did not persist. Version-1 contracts remain readable.
- A versioned extension API for independently distributed parser and semantic-checker plugins,
  including Python entry-point discovery, runtime registration for embedding, provider/version
  provenance, deterministic capability inventory, and exact compatibility rejection.
- Generic configured-source dispatch for external collateral formats, including forwarding of
  parser-specific options and the standard include/define/profile/column fields.

### Changed

- Newly generated contracts use schema version 2. Semantic checker plugins can inspect durable
  frozen view snapshots through `CheckerContext.contract.views` rather than requiring source
  collateral to be reparsed.

### Security

- Contract loading recomputes every view-snapshot digest and rejects stale or modified content.
  Snapshot attributes and extension values reject non-finite numbers, non-string object keys,
  unsupported values, and excessive nesting.
- External parsers cannot shadow built-in formats, aliases, or filename extensions. Parser plugin
  exceptions become fatal whole-view-tainted `OC9001` observations, and checker discovery,
  contract, or execution failures become fatal `OC9002` diagnostics.
- Package plugin discovery can be disabled for hermetic execution with
  `OPENCOLLATE_DISABLE_PLUGINS=1`.

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
- Parser-neutral observation families for design objects, clocks, interfaces, registers, fields,
  and structured view attributes.
- Cross-view checks for hierarchical references, clock targets and consistency, timing constraints,
  power intent, IP-XACT interface maps, and register maps.
- Strict source-option validation, configurable participation policies, authority selection, and
  schema-validated JSON reports/contracts.

### Changed

- OpenCollate now treats unknown, unsupported, tainted, and not-applicable facts as distinct states
  across the expanded model.
- The public UART example includes IP-XACT, SDC, UPF, C header, CDL, DEF, and GDSII views.

### Security

- XML entity/DTD declarations are rejected and no schema/network resolution is performed.
- Tcl-shaped input is parsed without executing Tcl.
- Integer expression evaluation is bounded and side-effect free.

## [0.1.0] - 2026-08-30

### Added

- Initial parser-neutral canonical model and deterministic diagnostic framework.
- Verilog/SystemVerilog, Liberty, LEF, and CSV pin-map importers.
- Component, port, shape, Boolean-function, and package-map consistency rules.
- Text, JSON, Markdown, and SARIF reporters.
- Multi-platform CI, branch coverage, package validation, CodeQL, dependency review, SBOMs,
  checksums, and release provenance.
